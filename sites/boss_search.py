"""跨公司搜索：持续接收页面自然加载的列表，结果只保留在内存。"""
import json
import time
from collections import deque
from urllib.parse import parse_qs, urlencode, urlsplit

from guard import LoginRequired, QueryError, check_page, signed_in
from browser import browser_error_details
from salary import summarize
from sites.boss import BossSession, HOME, JOBLIST, SchemaError, map_job, unpack
from sites.boss_locations import CITIES, city_code, location_matches, search_targets

SEARCH_CARDS = '.job-list-container .rec-job-list a[href*="/job_detail/"]:visible'
CITY_LIMIT = object()
PROGRESS_TIMEOUT = 20
STALL_BATCHES = 3


class CityReadError(SchemaError):
    """仅当前城市的加载停滞；登录、验证、取消等不属于可跳过错误。"""

    def __init__(self, reason, message):
        self.reason = reason
        super().__init__(message)


def request_page(request):
    """只识别明确的页码；缺少页码时用新增岗位判断进展。"""
    values = parse_qs(urlsplit(request.url).query)
    if getattr(request, 'method', None) == 'POST' and isinstance(request.post_data, str):
        try:
            body = json.loads(request.post_data)
        except ValueError:
            body = parse_qs(request.post_data)
        if isinstance(body, dict):
            values.update(body)
    for name in ('page', 'pageIndex', 'pageNum'):
        value = values.get(name)
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
    return None


def salary_summary(jobs):
    return summarize(dict(min=j["salary_min"], max=j["salary_max"], months=j["months"],
                          type=j["salary_type"]) for j in jobs)


def search_request_matches(request, page_url, keyword, code):
    """兼容 GET、表单和 JSON；未明文携带条件时核对当前搜索页 URL。"""
    query = parse_qs(urlsplit(request.url).query)
    if getattr(request, 'method', None) == 'POST':
        body = request.post_data
        if isinstance(body, str):
            try:
                values = json.loads(body)
            except ValueError:
                values = None
            if isinstance(values, dict):
                for name in ('query', 'city'):
                    value = values.get(name)
                    if type(value) in (str, int):
                        query.setdefault(name, [str(value)])
            else:
                for name, value in parse_qs(body).items():
                    query.setdefault(name, value)
    page = urlsplit(page_url)
    page_query = parse_qs(page.query)
    if page.hostname not in {'www.zhipin.com', 'zhipin.com'} or page.path not in {'/web/geek/job', '/web/geek/jobs'}:
        return False
    return all(query.get(name, page_query.get(name)) == [expected]
               for name, expected in (('query', keyword), ('city', code)))


class SearchSession(BossSession):
    def __init__(self, page, store, task):
        super().__init__(page, store, task)
        self.jobs = {}
        self.batches = 0
        self.scanned = 0
        self.params = None
        self.source = None
        self.sources = []
        self.targets = []
        self.cities_done = 0
        self.cities_visited = 0
        self.city_index = 0
        self.limited_cities = []
        self.failed_cities = []
        self.current_city = None
        self.province_search = False
        self.city_progress = []
        self.progress = ""
        self.guard.report = lambda message: self.task.report(
            f"{self.progress} · {message}" if self.progress else message)

    def update_city(self, status, message=""):
        if not self.province_search:
            return
        for item in self.city_progress:
            if item["city"] == self.current_city:
                item.update(status=status, message=message)
        self.task.set_city_progress(self.city_progress)

    def result(self, complete, reason, message):
        from datetime import datetime
        if self.params and self.params.get("selected_cities"):
            message = message.replace("全省", "所选城市")
        if any(item["status"] == "running" for item in self.city_progress):
            self.update_city("partial", message)
        rows = list(self.jobs.values())
        groups = {}
        for job in rows:
            ident = job.get("company_id") or job["company"]
            groups.setdefault(ident, []).append(job)
        companies = [dict(company=jobs[0]["company"], count=len(jobs), **salary_summary(jobs))
                     for jobs in groups.values()]
        companies.sort(key=lambda c: (-c["count"], c["company"]))
        if self.province_search:
            region = self.params['city'] + ("（所选城市）" if self.params.get("selected_cities") else "")
            message = (f"{region}：已查询 {self.cities_visited}/{len(self.targets)} 个城市，"
                       f"已读完 {self.cities_done}/{len(self.targets)} 个城市"
                       + (f"，当前 {self.current_city}" if not complete else "") + "。" + message)
        if self.failed_cities:
            message += " 未读完的异常城市：" + "；".join(
                f"{item['city']}（{item['message']}）" for item in self.failed_cities) + "。"
        return dict(jobs=rows, summary=salary_summary(rows), companies=companies,
                    company_count=len(companies), batches=self.batches, scanned=self.scanned,
                    mode="search", complete=complete, stop_reason=reason, stop_message=message,
                    source=self.source, filter_mode="site", query=self.params,
                    sources=list(self.sources), province_search=self.province_search,
                    cities_total=len(self.targets), cities_done=self.cities_done,
                    cities_visited=self.cities_visited, limited_cities=list(self.limited_cities),
                    failed_cities=[dict(item) for item in self.failed_cities],
                    current_city=self.current_city,
                    city_progress=[dict(item) for item in self.city_progress],
                    read_at=datetime.now().astimezone().isoformat(timespec="seconds"))

    def read_search(self, params):
        self.params = params
        region_code = city_code(params["city"])
        self.province_search = region_code.endswith("0000") and region_code != CITIES["全国"]
        self.targets = search_targets(params["city"])
        if params.get("selected_cities"):
            self.targets = [(city, code) for city, code in self.targets if city in params["selected_cities"]]
        self.city_progress = [dict(city=city, code=code, status="pending", message="")
                              for city, code in self.targets] if self.province_search else []
        limit = params.get("batches", 0)
        for index, (city, code) in enumerate(self.targets):
            if index:
                self.guard.action = f"准备查询下一城市：{city}（{index + 1}/{len(self.targets)}）"
                self.progress = self.guard.action
                self.task.report(self.guard.action)
                self.pause_page()
            self.current_city = city
            self.city_index = index + 1
            self.cities_visited = index + 1
            self.update_city("running")
            try:
                result = self._read_city(params, city, code)
            except CityReadError as exc:
                if not self.province_search:
                    raise
                self.failed_cities.append(dict(city=city, reason=exc.reason, message=str(exc)))
                self.update_city("partial", str(exc))
                self.task.report(f"{city}未读完：{exc}；已保留结果，继续其余城市")
                if limit and self.batches >= limit:
                    return self.result(False, "batch_limit", "已达到设置的总批数，尚未读完全省结果。")
                continue
            if result is CITY_LIMIT:
                self.limited_cities.append(city)
                self.update_city("partial", "达到省内每市批数上限，尚未读完")
            elif result is not None:
                return result
            else:
                self.cities_done += 1
                self.update_city("done", "已读完本次搜索提供的结果")
            if limit and self.batches >= limit and self.cities_done < len(self.targets):
                return self.result(False, "batch_limit", "已达到设置的总批数，尚未读完全省结果。")
        if self.failed_cities:
            message = "已查询全部目标城市，部分城市加载异常，结果不完整。"
            if self.limited_cities:
                message += " 达到每市上限的城市：" + "、".join(self.limited_cities) + "。"
            return self.result(False, "city_error", message)
        if self.limited_cities:
            return self.result(False, "city_limit", "已查询全部目标城市，但以下城市达到每市批数上限，尚未读完："
                               + "、".join(self.limited_cities) + "。可提高每市上限后重新查询。")
        return self.result(True, "end", "已读完本次搜索提供的结果（不代表平台全量岗位）。")

    def open_search(self, params, code, has_requests):
        """网站自身安全中转丢失条件时恢复一次；已发出的搜索、网络失败和风控不重试。"""
        from patchright.sync_api import Error as BrowserError
        interrupted = None
        try:
            self.goto(self.source)
        except BrowserError as exc:
            if browser_error_details(exc)['reason'] != 'navigation_interrupted':
                raise
            interrupted = exc
            # 只等待当前导航，不再加载页面。随后先检查受限/验证状态。
            self.page.wait_for_load_state('domcontentloaded', timeout=10000)
            self.before_action()
        current = urlsplit(self.page.url)
        values = parse_qs(current.query)
        security_return = (current.hostname in {'www.zhipin.com', 'zhipin.com'}
                           and current.path in {'/web/geek/job', '/web/geek/jobs'}
                           and '_security_check' in values)
        matches = values.get('query') == [params['keyword']] and values.get('city') == [code]
        if security_return and matches:
            return
        if (security_return and 'query' not in values and 'city' not in values
                and not has_requests() and signed_in(self.page)):
            self.before_action()
            self.task.report('网站跳转后未保留搜索条件，正在重新带入本次关键词和城市（仅一次）')
            self.goto(self.source)
            returned = urlsplit(self.page.url)
            returned_query = parse_qs(returned.query)
            if ('_security_check' in returned_query
                    and (returned_query.get('query') != [params['keyword']]
                         or returned_query.get('city') != [code])):
                raise QueryError('网站再次跳转且未保留搜索条件，已停止查询，不继续重复访问。')
            return
        if interrupted is not None:
            raise interrupted

    def _read_city(self, params, city, code):
        from patchright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout
        from browser import is_target_closed_error

        province = city if params.get("selected_cities") else params["city"] if self.province_search else ""
        prefix = f"{city}（{self.city_index}/{len(self.targets)} 城市） · " if self.province_search else ""
        self.source = HOME + "web/geek/jobs?" + urlencode({"query": params["keyword"], "city": code})
        self.sources.append(self.source)
        pending, responses = deque(), {}
        observed = {}
        seen = set()
        last_page = None
        no_new_batches = 0
        stuck_page_batches = 0
        failed_requests = set()
        city_batches = 0
        limit = params.get("batches", 0)
        city_limit = params.get("per_city_batches", 0) if self.province_search else 0
        operation = 'navigate'

        def observe_request(request):
            # 只接收本次监听后的搜索请求，排除推荐、公司页及明确不符的条件。
            url = urlsplit(request.url)
            query = parse_qs(url.query)
            if url.hostname in {"www.zhipin.com", "zhipin.com"} and url.path.startswith('/wapi/') and len(observed) < 60:
                observed[request] = dict(path=url.path, type=request.resource_type, status=None,
                                         query_matches=query.get('query') == [params['keyword']],
                                         city_matches=query.get('city') == [code])
            if (JOBLIST.search(request.url) and "/brand/" not in url.path
                    and search_request_matches(request, self.page.url, params["keyword"], code)):
                pending.append(request)

        def observe_response(response):
            if response.request in observed:
                observed[response.request]['status'] = response.status
            if response.request in pending:
                self.observe_risk(response)
                responses[response.request] = response

        def observe_failure(request):
            if request in pending:
                failed_requests.add(request)

        def observe_finished(request):
            response = responses.get(request)
            if response is not None:
                self.observe_risk(response, body_ready=True)

        def diagnose(error=None):
            diagnostic = dict(stage=self.guard.action, city=city, city_batches=city_batches,
                              unique_jobs=len(seen), last_page=last_page,
                              no_new_batches=no_new_batches, stuck_page_batches=stuck_page_batches,
                              requests=list(observed.values()))
            current = urlsplit(self.page.url)
            values = parse_qs(current.query)
            diagnostic['page_state'] = dict(
                on_boss=current.hostname in {'www.zhipin.com', 'zhipin.com'},
                on_search=current.path in {'/web/geek/job', '/web/geek/jobs'},
                security_return='_security_check' in values,
                query_matches=values.get('query') == [params['keyword']],
                city_matches=values.get('city') == [code])
            diagnostic['operation'] = operation
            if error is not None:
                diagnostic['browser_error'] = browser_error_details(error)
            try:
                # 只保存末尾链接的容器结构和尺寸，便于区分列表区/详情区；不保存文本或链接。
                diagnostic['scroll_containers'] = self.page.locator(SEARCH_CARDS).evaluate_all('''nodes =>
                    nodes.slice(-3).map(node => {
                        const chain = [];
                        for (let p = node.parentElement; p && chain.length < 7; p = p.parentElement)
                            chain.push({tag:p.tagName, class:p.className, height:p.clientHeight,
                                contentHeight:p.scrollHeight, overflow:getComputedStyle(p).overflowY});
                        return chain;
                    })''')
            except Exception:
                pass
            self.store.write('last_failure.json', diagnostic)

        self.page.on("request", observe_request)
        self.page.on("response", observe_response)
        self.page.on("requestfailed", observe_failure)
        self.page.on("requestfinished", observe_finished)
        try:
            self.guard.action = prefix + "打开跨公司岗位搜索"
            self.progress = self.guard.action
            self.task.report(self.guard.action)
            self.open_search(params, code, lambda: bool(pending) or city_batches > 0)
            operation = 'wait_for_list'
            deadline = time.monotonic() + PROGRESS_TIMEOUT
            login_check_after = time.monotonic() + 3
            next_scroll = 0
            while True:
                # 每轮先响应取消，并检查网站是否要求验证。
                self.task.sleep(0)
                check_page(self.page)
                if self.access_error:
                    raise self.access_error
                if failed_requests:
                    raise CityReadError("request_failed", "搜索请求失败，未确认读完")
                while pending and pending[0] in responses:
                    self.task.sleep(0)
                    check_page(self.page)
                    if self.access_error:
                        raise self.access_error
                    if time.monotonic() >= deadline:
                        raise CityReadError("timeout", "等待有效翻页进展超时，未确认读完")
                    response = responses.pop(pending.popleft())
                    if 500 <= response.status < 600:
                        raise CityReadError("server_error", f"列表响应 HTTP {response.status}，未确认读完")
                    self.check_status(response)
                    operation = 'read_response'
                    try:
                        data = unpack(response.json())
                    except (ValueError, UnicodeError) as exc:
                        raise SchemaError("搜索响应并非 JSON") from exc
                    if self.access_error:
                        raise self.access_error
                    operation = 'wait_for_list'
                    # 先校验整批，坏批次不会污染已经收集的结果。
                    batch = [map_job(item, {"name": ""}) for item in data["jobList"]]
                    ids = {job["url"] for job in batch}
                    new_count = len(ids - seen)
                    no_new_batches = 0 if new_count else no_new_batches + 1
                    page_number = request_page(response.request)
                    page_advanced = page_number is None or last_page is None or page_number > last_page
                    stuck_page_batches = 0 if page_advanced else stuck_page_batches + 1
                    if page_number is not None:
                        last_page = max(last_page, page_number) if last_page is not None else page_number
                    seen.update(ids)
                    self.scanned += len(batch)
                    self.batches += 1
                    city_batches += 1
                    for item, job in zip(data["jobList"], batch):
                        # 省份还须核对岗位城市归属，避免站点推荐或忽略条件时混入外省岗位。
                        # 搜索条件已在请求层核对；保留站点返回的相关岗位，不再按标题/技能二次过滤。
                        if location_matches(job["city"], province):
                            brand_id = item.get("encryptBrandId")
                            if isinstance(brand_id, str):
                                job["company_id"] = brand_id
                            self.jobs[job["url"]] = job
                    detail = f"本市 {city_batches}" + (f"/{city_limit}" if city_limit else "") + " 批 · "
                    self.progress = f"{prefix}已读 {self.batches} 批 · {detail}本批新增 {new_count} 个岗位 · {self.scanned} 个岗位 · {len(self.jobs)} 个匹配"
                    self.task.report(self.progress)
                    if data.get("hasMore") is False:
                        return None
                    if no_new_batches >= STALL_BATCHES:
                        raise CityReadError("no_new_jobs", f"连续 {STALL_BATCHES} 批返回相同岗位或空列表，未确认读完")
                    if stuck_page_batches >= STALL_BATCHES:
                        raise CityReadError("pagination_stalled", f"连续 {STALL_BATCHES} 批页码未前进，未确认读完")
                    if limit and self.batches >= limit:
                        return self.result(False, "batch_limit", "已达到设置的搜索批数，尚未确认读完。")
                    if city_limit and city_batches >= city_limit:
                        self.task.report(f"{prefix}已达到本市 {city_limit} 批上限，保留当前岗位；本市未读完")
                        return CITY_LIMIT
                    if not batch:
                        raise CityReadError("empty_batch", "搜索返回空批次但未声明结束，未确认读完")
                    pause_started = time.monotonic()
                    self.pause_page()
                    # 等待网站的期限不包含主动节流；重复响应不能重新获得完整期限。
                    deadline += time.monotonic() - pause_started
                    # 倒计时结束后立刻说明正在做什么，否则等到下一批响应前一直停在旧数字上。
                    self.task.report(f"{self.progress} · 滚动加载本市第 {city_batches + 1} 批")
                    if new_count and page_advanced:
                        deadline = time.monotonic() + PROGRESS_TIMEOUT
                    next_scroll = 0
                if self.access_error:
                    raise self.access_error
                # SPA 首屏可能短暂显示访客导航，先处理真实响应再判断登录。
                if (not pending and time.monotonic() >= login_check_after
                        and self.page.locator(".header-login-btn:visible").count()):
                    raise LoginRequired("登录已失效，请在 Chrome 窗口里手动登录。")
                if time.monotonic() >= deadline:
                    stage = f"{prefix}等待搜索第 {city_batches + 1} 批"
                    self.guard.action = stage
                    detail = "已保留收到的结果" if self.batches else "尚未收到有效列表"
                    raise CityReadError("timeout", f"{stage}超时，未确认读完；{detail}")
                # 虚拟列表可能已移除上一批的尾卡片；定位当前可见卡片，不按旧标题找元素。
                if city_batches and not pending and time.monotonic() >= next_scroll:
                    self.before_action()
                    self.guard.action = f"{prefix}滚动加载搜索第 {city_batches + 1} 批"
                    card = self.page.locator(SEARCH_CARDS).last
                    if not card.is_visible():
                        raise CityReadError("missing_cards", "找不到可见岗位卡片，无法继续滚动")
                    before = self.list_requests
                    operation = 'scroll'
                    card.hover(timeout=3000)
                    if before == self.list_requests:
                        self.before_action()
                        self.page.mouse.wheel(0, 800)
                    next_scroll = time.monotonic() + 1
                    operation = 'wait_for_list'
                self.page.wait_for_timeout(200)
        except BrowserTimeout as exc:
            if self.access_error:
                raise self.access_error
            check_page(self.page)
            diagnose(exc)
            raise CityReadError("timeout", "搜索页面操作超时，未确认读完") from exc
        except BrowserError as exc:
            if self.access_error:
                raise self.access_error
            if is_target_closed_error(exc):
                raise
            diagnose(exc)
            # 导航错误可能伴随跳转到受限页面，不能掩盖实际风控信号。
            try:
                check_page(self.page)
            except BrowserError:
                pass
            detail = browser_error_details(exc)
            if detail['reason'] in ('navigation_interrupted', 'navigation_context_changed'):
                message = '搜索页面发生跳转，未能确认本次关键词和城市，查询已停止。'
            elif detail['reason'] == 'response_unavailable':
                message = '浏览器未能读取已收到的岗位响应，查询已停止；已采集结果仍可导出。'
            else:
                code_label = f"（{detail['network_code']}）" if detail['network_code'] else ''
                message = f'搜索页面连接或响应读取失败{code_label}，未确认读完。'
            raise QueryError(message) from exc
        except QueryError:
            diagnose()
            raise
        finally:
            self.page.remove_listener("requestfinished", observe_finished)
            self.page.remove_listener("requestfailed", observe_failure)
            self.page.remove_listener("response", observe_response)
            self.page.remove_listener("request", observe_request)
