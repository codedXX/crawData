"""本地请求计数、查询间隔和需要人工处理的浏览器状态。"""
import json
import math
import random
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit


class QueryError(Exception):
    pass


class RiskBlocked(QueryError):
    def __init__(self, code, until=None):
        self.code = code
        self.until = until
        super().__init__("请在 Chrome 窗口里完成验证。")


class LoginRequired(QueryError):
    pass


class AccessRestricted(RiskBlocked):
    def __init__(self, until=None):
        super().__init__("account_restricted", until)


def retry_after_until(value, now=None):
    """解析 Retry-After 的秒数或 HTTP 日期；无效/过期值不缩短本地暂停。"""
    if not isinstance(value, str) or not value.strip():
        return None
    now = time.time() if now is None else now
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            until = now + int(value)
        else:
            date_value = parsedate_to_datetime(value)
            if date_value.tzinfo is None:
                date_value = date_value.replace(tzinfo=timezone.utc)
            until = date_value.timestamp()
        # 保证时间能持久化并在界面转换为北京时间，不让异常响应头破坏暂停处理。
        datetime.fromtimestamp(until, timezone(timedelta(hours=8)))
        return until if until > now else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def restriction_from_text(text):
    """只识别明确的账号限制通知，不把普通岗位文案当作风控。"""
    if not isinstance(text, str) or not ("访问受限" in text and re.search(r"[账帐][户号].*?(?:限制访问|异常行为)", text, re.S)):
        return None
    match = re.search(r"将于\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*恢复正常", text)
    until = None
    if match:
        try:
            until = datetime.strptime(match[1], "%Y-%m-%d %H:%M").replace(tzinfo=timezone(timedelta(hours=8))).timestamp()
        except ValueError:
            pass
    return AccessRestricted(until)


def restriction_page(page):
    url = urlsplit(page.url)
    return (url.hostname in {'www.zhipin.com', 'zhipin.com'}
            and url.path.lower() == '/web/passport/zp/403.html')


def check_restriction(page):
    try:
        text = page.locator('body').inner_text(timeout=1000)
    except Exception:
        text = ''
    error = restriction_from_text(text)
    if error:
        raise error
    # 风控页先跳转再渲染；正文空白或读取超时也不能按正常页继续。
    if restriction_page(page):
        raise AccessRestricted()


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


def login_link(page):
    return page.locator(LOGIN_LINK)


def signed_in(page):
    url = urlsplit(page.url)
    if (url.hostname not in {'www.zhipin.com', 'zhipin.com'}
            or url.path.lower().startswith(('/web/user', '/login', '/web/passport/'))):
        return False
    if login_link(page).count():
        return False
    # Cookie 存在只能说明曾保存过会话，不能证明服务器仍接受它。
    return page.locator('.user-nav .nav-figure img:visible, #header .nav-figure img:visible, '
                        '.user-nav img.avatar:visible, .user-nav .user-avatar:visible').count() > 0


def check_page(page):
    check_restriction(page)
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
            return dict(date=today, companies=0, requests=0, risks=0,
                        last_end=state.get("last_end", 0), needs_verify=state.get("needs_verify", False),
                        blocked_until=state.get("blocked_until", 0), block_reason=state.get("block_reason", ""))
        # 兼容已有额度文件：保留计数与验证状态，旧上限不再参与查询控制。
        state.pop("company_limit", None)
        state.pop("request_limit", None)
        return state

    def check(self):
        state = self.state()
        if state.get("blocked_until", 0) > time.time():
            raise QueryError(self.access_status()["message"])
        return state

    def access_status(self):
        state = self.state()
        until = state.get("blocked_until", 0)
        blocked = until > time.time()
        when = datetime.fromtimestamp(until, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M') if blocked else ''
        return dict(blocked=blocked, blocked_until=until,
                    message=(f"{state.get('block_reason') or '检测到访问限制'}，已暂停新查询至 {when}（北京时间）。"
                             "期间不会访问 BOSS，已有结果仍可导出；到时不会自动重试。" if blocked else ""))

    def interval(self, low, high):
        duration = min(high, max(low, random.gauss((low + high) / 2, (high - low) / 6)))
        return duration * (2 if self.state()["risks"] else 1)

    def wait(self, seconds):
        total = float(seconds)
        if total <= 0:
            return
        # 只报一次计划时长，用时由界面按查询开始时刻自己算，避免每批都刷屏。
        # 向上取整，显示值不会少于实际要等的时间。
        self.report(f"等待 {math.ceil(total)} 秒")
        self.sleep(total)

    def start_company(self):
        state = self.check()
        if state["last_end"]:
            delay = self.interval(60, 120)
            if state["companies"] and state["companies"] % 5 == 0 and state.get("rest_after") != state["companies"]:
                delay += self.interval(180, 300)
            self.wait(delay - (time.time() - state["last_end"]))
        state = self.check()
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

    def record_risk(self, code, until=None):
        state = self.state()
        state["risks"] += 1
        state["needs_verify"] = True
        minimum = time.time() + (86400 if code == "account_restricted" and until is None else 1800)
        state["blocked_until"] = max(state.get("blocked_until", 0), until or 0, minimum)
        state["block_reason"] = "BOSS 账号访问受限" if code == "account_restricted" else "检测到安全验证或访问频率限制"
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
