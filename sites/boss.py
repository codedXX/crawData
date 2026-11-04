"""仅通过页面发起的列表响应读取 BOSS 岗位。"""
import random
import re
import time
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from guard import Guard, LoginRequired, QueryError, RiskBlocked, challenged, check_page, login_link, signed_in
from salary import parse_salary, summarize
from store import ROOT, key, write_json

HOME = "https://www.zhipin.com/"
JOBLIST = re.compile(
    r"^https://(?:www\.)?zhipin\.com/wapi/zpgeek/"
    r"(?:[^?#]*joblist\.json|brand/job/querylist\.json)(?:[?#]|$)", re.I
)
ID = re.compile(r"[A-Za-z0-9_~-]+")
BRAND_PATH = re.compile(rf"/gongsi/(?:job/)?({ID.pattern})\.html/?")
# 公司页箭头本身是无尺寸的 <i>，要定位其可点击的父级 <a>。
NEXT = '.options-pages a:has(.arrow-right), .ui-icon-arrow-right, .options-pages a.next, .pagination .next, button[aria-label="下一页"], a[aria-label="下一页"]'
COMPANY_DOC = re.compile(r"^https://www\.zhipin\.com/gongsi/job/[A-Za-z0-9_~-]+\.html(?:[?#]|$)")
FILTER = 'input[placeholder="查找职位中包含的关键词"], .position-search input[placeholder*="职位"], .job-filter input[placeholder*="职位"]'
FIELDS = ("jobName", "salaryDesc", "brandName", "encryptBrandId", "encryptJobId",
          "cityName", "areaDistrict", "jobExperience", "jobDegree", "skills")
SAMPLE = ROOT / "examples" / "boss_joblist.json"
FRONTEND_TECH = re.compile(r"(?<![a-z0-9])(?:web|h5|react|vue)(?![a-z0-9])|小程序", re.I)
FRONTEND_EXCLUDE = re.compile(r"后端|后台|运营|研究员|全栈|web\s*3|back[ -]?end|full[ -]?stack", re.I)
DEVELOPER = re.compile(r"开发|工程师|developer|engineer", re.I)
RISK_MESSAGE = re.compile(r"访问(?:行为)?异常|行为异常|访问过于频繁|请求过于频繁|安全验证|人机验证|滑块验证|验证码|操作过于频繁", re.I)


class SchemaError(QueryError):
    def __init__(self, detail):
        super().__init__(f"接口结构变化或列表未加载（{detail}）。请对照 Chrome 页面和样本检查。")


def company_id(value):
    if "://" not in value and not value.startswith(("www.", "//")):
        return None
    url = urlsplit(value if "://" in value else "https://" + value.removeprefix("//"))
    match = BRAND_PATH.fullmatch(url.path)
    if url.scheme not in {"http", "https"} or url.netloc not in {"www.zhipin.com", "zhipin.com"} or not match:
        raise QueryError("请输入 BOSS 直聘的公司主页或公司职位页链接。")
    return match[1]


def unpack(body):
    if not isinstance(body, dict) or "code" not in body:
        raise SchemaError("缺少 code")
    if body["code"] != 0:
        if re.search(r"未登录|请.*登录|登录.*失效", str(body.get("message", ""))):
            raise LoginRequired("登录已失效，请在 Chrome 窗口里手动登录。")
        if RISK_MESSAGE.search(str(body.get("message", ""))):
            raise RiskBlocked(body["code"] if isinstance(body["code"], int) else "api-error")
        raise QueryError(f"列表接口返回错误（code={body['code']}），查询已停止。")
    data = body.get("zpData")
    if not isinstance(data, dict) or not isinstance(data.get("jobList"), list):
        raise SchemaError("缺少 zpData.jobList")
    if not all(isinstance(item, dict) for item in data["jobList"]):
        raise SchemaError("jobList 格式不符")
    if "hasMore" in data and not isinstance(data["hasMore"], bool):
        raise SchemaError("hasMore 格式不符")
    return data


def candidates(data):
    brands = {}
    for item in data["jobList"]:
        name, ident = item.get("brandName"), item.get("encryptBrandId")
        if not isinstance(name, str) or not name or not isinstance(ident, str) or not ID.fullmatch(ident):
            raise SchemaError("公司名称或 ID 缺失")
        brands[ident] = dict(name=name, id=ident)
    return list(brands.values())


def map_job(item, brand):
    ident = item.get("encryptJobId")
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise SchemaError("缺少 encryptJobId")
    def field(name, default=""):
        value = item.get(name, default)
        if value is None:
            return default
        if not isinstance(value, str):
            raise SchemaError(f"{name} 格式不符")
        return value.strip()
    title, company = field("jobName"), field("brandName", brand["name"])
    if not title or not company or "salaryDesc" not in item:
        raise SchemaError("岗位名、公司名或 salaryDesc 缺失")
    salary = parse_salary(None if item["salaryDesc"] is None else field("salaryDesc"))
    skills = item.get("skills") or []
    if not isinstance(skills, list) or not all(isinstance(s, str) for s in skills):
        raise SchemaError("skills 格式不符")
    return dict(title=title, company=company, salary_raw=salary["raw"], salary_min=salary["min"],
                salary_max=salary["max"], months=salary["months"], salary_type=salary["type"],
                skills=skills, city=field("cityName"), area=field("areaDistrict"),
                experience=field("jobExperience"), degree=field("jobDegree"),
                url=f"{HOME}job_detail/{ident}.html")


def save_sample(data, path=SAMPLE):
    if path.exists() or not data["jobList"]:
        return
    # 仅保存可回放的岗位字段。未知嵌套对象、招聘者资料及跟踪参数均不写盘。
    rows = []
    for item in data["jobList"]:
        row = {k: item[k] for k in FIELDS if k in item and (item[k] is None or isinstance(item[k], str))}
        if isinstance(item.get("skills"), list):
            row["skills"] = [s for s in item["skills"] if isinstance(s, str)]
        rows.append(row)
    sample = dict(jobList=rows)
    if "hasMore" in data:
        sample["hasMore"] = data["hasMore"]
    write_json(path, dict(code=0, zpData=sample))


def matches(job, keyword, city):
    text = " ".join([job["title"], *job["skills"]])
    title = key(job["title"])
    if key(keyword) == "前端":
        by_role = "前端" in title or (
            FRONTEND_TECH.search(title) and DEVELOPER.search(title) and not FRONTEND_EXCLUDE.search(title)
        )
    else:
        by_role = key(keyword) in key(text)
    return bool(by_role) and (not city or key(job["city"]).removesuffix("市") == key(city).removesuffix("市"))


class BossSession:
    """一次实时查询，所有操作都在浏览器所属线程运行。"""
    def __init__(self, page, store, task):
        self.page = page
        self.store = store
        self.task = task
        self.guard = Guard(store, task.report, task.sleep)
        self.access_error = None
        self.list_requests = 0

    def meter(self, request):
        if not (JOBLIST.search(request.url) or
                (COMPANY_DOC.search(request.url) and request.resource_type == "document")):
            return
        self.list_requests += 1
        try:
            self.guard.list_request()
        except (QueryError, OSError, ValueError) as exc:
            self.access_error = exc

    def before_action(self):
        if self.access_error:
            raise self.access_error
        self.guard.check()
        self.task.sleep(0)
        check_page(self.page)

    def ask(self, kind, message, choices=None):
        return self.task.ask(kind, message, choices, pump=lambda: self.page.wait_for_timeout(80))

    def verify(self):
        for _ in range(3):
            self.ask("verify", "请在此项目打开的 Chrome 窗口里完成验证，然后点击继续。")
            if not challenged(self.page):
                self.guard.clear_verification()
                return
        raise QueryError("连续 3 次仍检测到安全验证，请检查 Chrome 页面后重新提交。")

    def goto(self, url):
        self.before_action()
        self.page.goto(url, wait_until="domcontentloaded", timeout=20000)
        check_page(self.page)

    def login(self):
        page = self.page
        self.guard.action = "登录"
        if not page.url.startswith(HOME):
            # 新标签页无法反映上次验证的结果；先打开首页再判断。
            self.guard.check()
            page.goto(HOME, wait_until="domcontentloaded", timeout=20000)
        if self.guard.state()["needs_verify"]:
            if challenged(page):
                self.verify()
            else:
                self.guard.clear_verification()
        if challenged(page):
            raise RiskBlocked("verification")
        for attempt in range(3):
            if signed_in(page):
                return
            if attempt == 0:
                self.open_login()
            self.task.ask("login", "已在 Chrome 打开登录页，请用 BOSS App 扫码。登录完成后会自动继续，也可点击“我已登录”。",
                          pump=lambda: page.wait_for_timeout(80), done=lambda: signed_in(page))
            if challenged(page):
                raise RiskBlocked("verification")
        if not signed_in(page):
            raise QueryError("连续 3 次未能确认登录。若 Chrome 已显示头像，可能是页面结构变化，请检查登录选择器后再查询。")

    def open_login(self):
        """只点开登录入口，扫码由用户完成；找不到入口时保持当前页面。"""
        link = login_link(self.page).first
        try:
            if link.count():
                self.task.sleep(random.uniform(0.8, 2.0))
                link.click(timeout=5000)
        except QueryError:
            raise
        except Exception:
            # 页面结构变化时退回手动点击。
            pass

    def collect(self, label, action, source=None):
        from patchright.sync_api import TimeoutError as BrowserTimeout
        from sites.boss_ssr import company_list

        self.guard.check()
        self.guard.action = label
        self.task.report(label)
        responses = []
        initiated = set()
        observed = {}

        def observe_request(request):
            initiated.add(request)
            url = urlsplit(request.url)
            if (url.hostname in {"www.zhipin.com", "zhipin.com"}
                    and (url.path.startswith("/wapi/") or request.resource_type == "document")
                    and len(observed) < 40):
                observed[request] = dict(path=url.path, type=request.resource_type, status=None)

        def observe(response):
            if response.request not in initiated:
                return
            if response.request in observed:
                observed[response.request]["status"] = response.status
            if JOBLIST.search(response.url):
                responses.append(response)
            elif (source and response.request.resource_type == "document"
                  and response.url.split("?")[0] == source and response.status >= 200
                  and not 300 <= response.status < 400):
                responses.append(response)

        self.page.on("request", observe_request)
        self.page.on("response", observe)
        deadline = time.monotonic() + 15
        try:
            self.before_action()
            action()
            while True:
                if self.access_error:
                    raise self.access_error
                check_page(self.page)
                if self.page.locator(".header-login-btn:visible").count():
                    raise LoginRequired("请在 Chrome 窗口里登录。")
                while responses:
                    response = responses.pop(0)
                    self.check_status(response)
                    if JOBLIST.search(response.url):
                        try:
                            data = unpack(response.json())
                        except (ValueError, UnicodeError) as exc:
                            raise SchemaError("响应并非 JSON") from exc
                        sample = ("boss_company_querylist.json" if urlsplit(response.url).path == "/wapi/zpgeek/brand/job/querylist.json"
                                  else "boss_joblist.json")
                        save_sample(data, self.store.samples / sample)
                        return data
                    data = company_list(response.text())
                    if data is not None:
                        data = unpack({"code": 0, "zpData": data})
                        save_sample(data, self.store.samples / "boss_company_first_page.json")
                        return data
                if time.monotonic() >= deadline:
                    self.store.write("last_failure.json", dict(stage=label, requests=list(observed.values())))
                    raise SchemaError(f"{label}：等待列表响应超时，且未读到公司首屏数据；诊断已保存到 {self.store.directory / 'last_failure.json'}")
                self.task.sleep(0)
                self.page.wait_for_timeout(200)
        except BrowserTimeout as exc:
            check_page(self.page)
            raise SchemaError("页面操作超时") from exc
        finally:
            self.page.remove_listener("response", observe)
            self.page.remove_listener("request", observe_request)

    @staticmethod
    def check_status(response):
        if response.status == 429:
            raise RiskBlocked(response.status)
        if response.status == 401:
            raise LoginRequired("登录已失效。")
        if not response.ok:
            raise QueryError(f"列表响应 HTTP {response.status}，查询已停止。")

    def resolve(self, company):
        url = HOME + "web/geek/job?" + urlencode({"query": company})
        choices = candidates(self.collect("查找候选公司", lambda: self.goto(url)))
        if not choices:
            raise QueryError("没有找到候选公司。请更换公司名称，或粘贴公司主页链接。")
        ident = self.ask("choose", "请选择要查询的公司。", choices)
        brand = next(b for b in choices if b["id"] == ident)
        self.store.remember(company, brand)
        return brand

    def pause_page(self):
        self.guard.wait(self.guard.interval(4, 9))

    def next_action(self, data):
        button = self.page.locator(NEXT).last
        if not button.is_visible():
            if data.get("hasMore") is True:
                if not data["jobList"]:
                    raise SchemaError("列表为空但仍声明有下一页")
                def scroll_list():
                    self.before_action()
                    before = self.list_requests
                    # 公司页使用滚动加载；先将指针移到岗位卡片，避免滚动右侧职位详情。
                    self.page.get_by_text(data["jobList"][-1]["jobName"], exact=True).first.hover(timeout=5000)
                    # hover 的自动滚动可能已经触发下一页，避免重复加载。
                    if self.list_requests == before:
                        self.before_action()
                        self.page.mouse.wheel(0, 1900)
                return scroll_list
            raise SchemaError("缺少 hasMore 且没有下一页按钮")
        control = button.locator("xpath=ancestor-or-self::*[self::a or self::button or self::li][1]")
        if control.count():
            button = control
        if not button.is_enabled() or "disabled" in (button.get_attribute("class") or ""):
            return None
        def advance():
            before = self.list_requests
            for _ in range(2):
                self.before_action()
                self.page.mouse.wheel(0, 520)
                self.page.wait_for_timeout(int(self.guard.interval(1, 2) * 1000))
                check_page(self.page)
                if self.list_requests != before:
                    return
            self.before_action()
            button.click(timeout=5000)
        return advance

    def read(self, brand, params):
        pages, keyword = params["pages"], params["keyword"]
        source = f"{HOME}gongsi/job/{brand['id']}.html"
        data = self.collect("打开公司职位页", lambda: self.goto(source), source)
        if urlsplit(self.page.url).path != urlsplit(source).path:
            raise SchemaError("未到达所选公司的职位页")
        jobs = {}
        used = 1
        search = self.page.locator(FILTER).first
        filter_mode = "none"
        if keyword:
            filter_mode = "local"
            if search.is_visible():
                self.pause_page()
                self.before_action()
                search.press("ControlOrMeta+A")
                search.press("Backspace")
                for char in keyword[:-1]:
                    self.before_action()
                    search.press_sequentially(char, delay=self.guard.interval(.08, .2) * 1000)
                def filter_page():
                    before = self.list_requests
                    search.press_sequentially(keyword[-1], delay=self.guard.interval(.08, .2) * 1000)
                    if self.list_requests == before:
                        self.before_action()
                        search.press("Enter")
                data = self.collect(f"筛选结果第 1/{pages} 页", filter_page, source)
                filter_mode = "site"
        self.task.report(f"第 1/{pages} 页")
        while True:
            for item in data["jobList"]:
                if item.get("encryptBrandId") and item["encryptBrandId"] != brand["id"]:
                    continue
                job = map_job(item, brand)
                if matches(job, keyword, params["city"]):
                    jobs[job["url"]] = job
            if used >= pages or data.get("hasMore") is False:
                break
            advance = self.next_action(data)
            if advance is None:
                break
            self.pause_page()
            used += 1
            previous = data["jobList"]
            data = self.collect(f"第 {used}/{pages} 页", advance, source)
            if previous and data["jobList"] == previous:
                raise SchemaError("翻页后仍返回相同岗位")
        rows = list(jobs.values())
        summary = summarize(dict(min=j["salary_min"], max=j["salary_max"], months=j["months"],
                                 type=j["salary_type"]) for j in rows)
        return dict(jobs=rows, summary=summary, pages=used, source=source, filter_mode=filter_mode,
                    read_at=datetime.now().astimezone().isoformat(timespec="seconds"), query=params)


class BossQuery:
    def __init__(self, browser, store):
        self.browser = browser
        self.store = store

    def run(self, params, task):
        company = params["company"]
        ident = company_id(company)
        brand = dict(id=ident, name="") if ident else None
        if not ident and not params.get("reselect"):
            brand = self.store.company(company)
        Guard(self.store, task.report).check(companies=True)
        session = BossSession(self.browser.page(), self.store, task)
        page, guard = session.page, session.guard
        started = False
        success = False
        page.on("request", session.meter)
        try:
            session.login()
            guard.start_company()
            started = True
            if not brand:
                brand = session.resolve(company)
                session.pause_page()
            result = session.read(brand, params)
            success = True
            return result
        except RiskBlocked as exc:
            if guard.record_risk(exc.code) >= 2:
                raise QueryError("当天第二次触发验证，已锁定查询，请明天再试。") from exc
            session.verify()
            raise QueryError("已确认验证，原查询已停止。如需重新查询，请再次提交。") from exc
        except LoginRequired as exc:
            session.ask("login", f"{exc} 完成后点击“我已登录”。")
            if not signed_in(page):
                raise QueryError("尚未检测到登录头像。请在 Chrome 完成登录后重新查询。") from exc
            raise QueryError("已确认登录，请重新提交查询。") from exc
        finally:
            try:
                page.remove_listener("request", session.meter)
            finally:
                if started:
                    guard.finish_company(success=success)
