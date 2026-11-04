"""跨公司搜索：持续接收页面自然加载的列表，结果只保留在内存。"""
import json
import time
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from guard import LoginRequired, QueryError, check_page
from salary import summarize
from sites.boss import BossSession, HOME, JOBLIST, SchemaError, map_job, matches, unpack

# 官方公开城市字典的名称/代码快照，来源和更新时间见 README。
CITIES = json.loads(Path(__file__).with_name("boss_cities.json").read_text(encoding="utf-8"))
SEARCH_CARDS = '.job-list-container .rec-job-list a[href*="/job_detail/"]:visible'


def city_code(city):
    name = city.strip()
    if not name or name in {"全国", "不限", "不限城市"}:
        return CITIES["全国"]
    code = CITIES.get(name) or CITIES.get(name.removesuffix("市"))
    if not code:
        raise ValueError("未识别城市，请填写完整城市名（例如深圳、北京市），或留空搜索全国。")
    return code


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

    def result(self, complete, reason, message):
        from datetime import datetime
        rows = list(self.jobs.values())
        groups = {}
        for job in rows:
            ident = job.get("company_id") or job["company"]
            groups.setdefault(ident, []).append(job)
        companies = [dict(company=jobs[0]["company"], count=len(jobs), **salary_summary(jobs))
                     for jobs in groups.values()]
        companies.sort(key=lambda c: (-c["count"], c["company"]))
        return dict(jobs=rows, summary=salary_summary(rows), companies=companies,
                    company_count=len(companies), batches=self.batches, scanned=self.scanned,
                    mode="search", complete=complete, stop_reason=reason, stop_message=message,
                    source=self.source, filter_mode="site", query=self.params,
                    read_at=datetime.now().astimezone().isoformat(timespec="seconds"))

    def read_search(self, params):
        from patchright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout
        from browser import is_target_closed_error

        self.params = params
        code = city_code(params["city"])
        self.source = HOME + "web/geek/jobs?" + urlencode({"query": params["keyword"], "city": code})
        pending, responses = deque(), {}
        observed = {}
        seen = set()
        limit = params.get("batches", 0)

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
                responses[response.request] = response

        def diagnose():
            diagnostic = dict(stage=self.guard.action, requests=list(observed.values()))
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
        try:
            self.guard.action = "打开跨公司岗位搜索"
            self.task.report(self.guard.action)
            self.goto(self.source)
            deadline = time.monotonic() + 20
            login_check_after = time.monotonic() + 3
            next_scroll = 0
            while True:
                # 每轮先响应取消，并检查网站是否要求验证。
                self.task.sleep(0)
                check_page(self.page)
                while pending and pending[0] in responses:
                    response = responses.pop(pending.popleft())
                    self.check_status(response)
                    try:
                        data = unpack(response.json())
                    except (ValueError, UnicodeError) as exc:
                        raise SchemaError("搜索响应并非 JSON") from exc
                    # 先校验整批，坏批次不会污染已经收集的结果。
                    batch = [map_job(item, {"name": ""}) for item in data["jobList"]]
                    ids = {job["url"] for job in batch}
                    if ids and ids <= seen and data.get("hasMore") is not False:
                        raise SchemaError("滚动后反复返回相同岗位，未确认读完")
                    seen.update(ids)
                    self.scanned += len(batch)
                    self.batches += 1
                    for item, job in zip(data["jobList"], batch):
                        # 城市已作为站内条件提交；“全国”不再作为本地城市名过滤。
                        if matches(job, params["keyword"], ""):
                            brand_id = item.get("encryptBrandId")
                            if isinstance(brand_id, str):
                                job["company_id"] = brand_id
                            self.jobs[job["url"]] = job
                    self.task.report(f"已读 {self.batches} 批 · {self.scanned} 个岗位 · {len(self.jobs)} 个匹配")
                    if data.get("hasMore") is False:
                        return self.result(True, "end", "已读完本次搜索提供的结果（不代表平台全量岗位）。")
                    if limit and self.batches >= limit:
                        return self.result(False, "batch_limit", "已达到设置的搜索批数，尚未确认读完。")
                    if not batch:
                        raise SchemaError("搜索返回空批次但未声明结束，未确认读完")
                    self.pause_page()
                    deadline = time.monotonic() + 20
                    next_scroll = 0
                if self.access_error:
                    raise self.access_error
                # SPA 首屏可能短暂显示访客导航，先处理真实响应再判断登录。
                if (not pending and time.monotonic() >= login_check_after
                        and self.page.locator(".header-login-btn:visible").count()):
                    raise LoginRequired("登录已失效，请在 Chrome 窗口里手动登录。")
                if time.monotonic() >= deadline:
                    stage = f"等待搜索第 {self.batches + 1} 批"
                    self.guard.action = stage
                    detail = "已保留收到的结果" if self.batches else "尚未收到有效列表"
                    raise SchemaError(f"{stage}超时，未确认读完；{detail}")
                # 虚拟列表可能已移除上一批的尾卡片；定位当前可见卡片，不按旧标题找元素。
                if self.batches and not pending and time.monotonic() >= next_scroll:
                    self.before_action()
                    self.guard.action = f"滚动加载搜索第 {self.batches + 1} 批"
                    card = self.page.locator(SEARCH_CARDS).last
                    if not card.is_visible():
                        raise SchemaError("找不到可见岗位卡片，无法继续滚动")
                    before = self.list_requests
                    card.hover(timeout=3000)
                    if before == self.list_requests:
                        self.before_action()
                        self.page.mouse.wheel(0, 800)
                    next_scroll = time.monotonic() + 1
                self.page.wait_for_timeout(200)
        except BrowserTimeout as exc:
            check_page(self.page)
            diagnose()
            raise SchemaError("搜索页面操作超时，未确认读完") from exc
        except BrowserError as exc:
            if is_target_closed_error(exc):
                raise
            diagnose()
            raise QueryError("搜索页面连接或响应读取失败，未确认读完。") from exc
        except QueryError:
            diagnose()
            raise
        finally:
            self.page.remove_listener("response", observe_response)
            self.page.remove_listener("request", observe_request)
