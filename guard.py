"""本地日期额度、查询间隔和需要人工处理的浏览器状态。"""
import json
import random
import time
from datetime import date, datetime
from urllib.parse import urlsplit


class QueryError(Exception):
    pass


class RiskBlocked(QueryError):
    def __init__(self, code):
        self.code = code
        super().__init__("请在 Chrome 窗口里完成验证。")


class LoginRequired(QueryError):
    pass


TOKEN_PAGE = "/web/passport/zp/security.html"


def challenged(page, settle=10000):
    path = urlsplit(page.url).path.lower()
    if path == TOKEN_PAGE:
        # BOSS 刷新令牌的中转页（带 seed/name 参数），通常几秒内自动跳回原页，不是验证码。
        try:
            page.wait_for_url(lambda url: urlsplit(url).path.lower() != TOKEN_PAGE, timeout=settle)
        except Exception:
            pass
        path = urlsplit(page.url).path.lower()
    return ("verify" in path or "security" in path or
            page.locator('iframe[src*="captcha"]:visible, iframe[src*="verify"]:visible, '
                         '.geetest_panel:visible, #nc_1_wrapper:visible, .verify-wrap:visible').count() > 0)


LOGIN_LINK = 'a:text-is("登录/注册"):visible, .header-login-btn:visible'
# BOSS 登录后写入的会话 Cookie；名称来自公开资料，首次实测时确认。
SESSION_COOKIES = {"wt2", "zp_at"}


def login_link(page):
    return page.locator(LOGIN_LINK)


def signed_in(page):
    if login_link(page).count():
        return False
    if page.locator('.user-nav .nav-figure img:visible, #header .nav-figure img:visible, '
                    '.user-nav img.avatar:visible, .user-nav .user-avatar:visible').count():
        return True
    try:
        cookies = page.context.cookies("https://www.zhipin.com/")
    except Exception:
        return False
    return any(c.get("name") in SESSION_COOKIES and c.get("value") for c in cookies)


def check_page(page):
    if challenged(page):
        raise RiskBlocked("verification")
    if "/login" in urlsplit(page.url).path.lower():
        raise LoginRequired("请在 Chrome 窗口里手动登录。")


class Guard:
    def __init__(self, store, report, sleep=time.sleep):
        self.store = store
        self.report = report
        self.sleep = sleep
        self.action = "登录"

    def state(self):
        state = self.store.read("quota.json")
        today = date.today().isoformat()
        if state.get("date") != today:
            return dict(date=today, companies=0, requests=0, company_limit=20,
                        request_limit=60, risks=0, last_end=0, needs_verify=False)
        return state

    def check(self, companies=False):
        state = self.state()
        if state["risks"] >= 2:
            raise QueryError("当天已两次触发验证，查询已锁定，请明天再试。")
        if state["requests"] >= state["request_limit"] or (
                companies and state["companies"] >= state["company_limit"]):
            raise QueryError("当天额度已用完，请明天再试。")
        return state

    def interval(self, low, high):
        duration = min(high, max(low, random.gauss((low + high) / 2, (high - low) / 6)))
        return duration * (2 if self.state()["risks"] else 1)

    def wait(self, seconds):
        if seconds > 0:
            self.report(f"等待 {seconds:.0f} 秒")
            self.sleep(seconds)

    def start_company(self):
        state = self.check(companies=True)
        if state["last_end"]:
            delay = self.interval(20, 60)
            if state["companies"] and state["companies"] % 5 == 0 and state.get("rest_after") != state["companies"]:
                delay += self.interval(180, 300)
            self.wait(delay - (time.time() - state["last_end"]))
        state = self.check(companies=True)
        state["rest_after"] = state["companies"]
        state["last_end"] = time.time()
        self.store.write("quota.json", state)

    def finish_company(self, success=False):
        state = self.state()
        if success:
            state["companies"] += 1
        state["last_end"] = time.time()
        self.store.write("quota.json", state)

    def list_request(self):
        # 被动记录实际发出的请求，包括失败请求和页面自动发出的额外请求。
        state = self.state()
        state["requests"] += 1
        self.store.write("quota.json", state)
        if state["requests"] > state["request_limit"]:
            raise QueryError("页面额外加载已达到当天请求额度，查询已停止，请明天再试。")

    def record_risk(self, code):
        state = self.state()
        state["risks"] += 1
        state["needs_verify"] = True
        if state["risks"] == 1:
            for used, limit in (("requests", "request_limit"), ("companies", "company_limit")):
                state[limit] = state[used] + max(0, state[limit] - state[used]) // 2
        self.store.write("quota.json", state)
        event = dict(time=datetime.now().astimezone().isoformat(timespec="seconds"),
                     action=self.action, code=code)
        with (self.store.directory / "events.log").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return state["risks"]

    def clear_verification(self):
        state = self.state()
        state["needs_verify"] = False
        self.store.write("quota.json", state)
