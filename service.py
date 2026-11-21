"""网页和 CLI 共用的单线程查询队列与人工确认流程。"""
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from browser import Browser, is_target_closed_error
from guard import Guard, QueryError, RiskBlocked, AccessRestricted, LoginRequired, check_page, signed_in
from accounts import AccountProfiles
from sites.boss import BossQuery, company_id
from store import DATA, ROOT, Store


class Busy(QueryError):
    pass


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("查询参数应为对象。")
    params = {}
    for name, default, limit in (("company", "", 500), ("keyword", "前端", 100), ("city", "", 80)):
        value = data.get(name, default)
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("公司、关键词或地区格式无效。")
        params[name] = value.strip()
    if not params["company"] and not params["keyword"]:
        raise ValueError("跨公司搜索请填写岗位关键词。")
    company_id(params["company"])
    from sites.boss_locations import city_code
    city_code(params["city"])
    if "selected_cities" in data:
        from sites.boss_locations import selected_cities
        params["selected_cities"] = selected_cities(params["city"], data["selected_cities"])
    pages = data.get("pages", 1)
    if type(pages) is not int or not 1 <= pages <= 5:
        raise ValueError("每次读取 1 至 5 页。")
    reselect = data.get("reselect", False)
    if type(reselect) is not bool:
        raise ValueError("重新选择公司参数必须为布尔值。")
    if reselect and not params["company"]:
        raise ValueError("跨公司搜索不能重新选择同名公司。")
    company_details = data.get('company_details', False)
    if type(company_details) is not bool:
        raise ValueError('读取工商信息参数必须为布尔值。')
    batches = data.get("batches", 0)
    if type(batches) is not int or not 0 <= batches <= 60:
        raise ValueError("搜索批数应为 0（自动加载）或 1 至 60。")
    per_city_batches = data.get("per_city_batches", 0)
    if type(per_city_batches) is not int or not 0 <= per_city_batches <= 60:
        raise ValueError("省内每市批数应为 0（不限）或 1 至 60。")
    return dict(params, pages=pages, batches=batches, per_city_batches=per_city_batches,
                reselect=reselect, company_details=company_details)


class Task:
    def __init__(self, params):
        self.id = uuid.uuid4().hex
        self.condition = threading.Condition()
        self.cancelled = threading.Event()
        self.answer = None
        self.export_task = None
        # started_at 供界面显示整次查询的用时；finish() 时补 finished_at 把计时定住。
        self.state = dict(id=self.id, query=params, status="running", message="准备查询",
                          started_at=time.time(), finished_at=None)
        if params.get("city") and not params.get("company"):
            from sites.boss_locations import CITIES, city_code, search_targets
            code = city_code(params["city"])
            if code.endswith("0000") and code != CITIES["全国"]:
                self.state["city_progress"] = [dict(city=city, code=code, status="pending", message="")
                                               for city, code in search_targets(params["city"])
                                               if not params.get("selected_cities") or city in params["selected_cities"]]

    def set_city_progress(self, cities):
        with self.condition:
            self.state["city_progress"] = [dict(item) for item in cities]

    def set_phase(self, phase):
        with self.condition:
            self.state['phase'] = phase

    def set_company_details_progress(self, checked, total):
        with self.condition:
            self.state['company_details_progress'] = dict(checked=checked, total=total)

    def snapshot(self):
        with self.condition:
            state = self.state.copy()
            if self.export_task:
                state["export"] = self.export_task.snapshot()
            return state

    def report(self, message):
        if self.cancelled.is_set():
            raise QueryError("查询已取消。")
        with self.condition:
            self.state.update(message=message)

    def sleep(self, seconds):
        if self.cancelled.wait(seconds):
            raise QueryError("查询已取消。")

    def ask(self, kind, message, choices=None, pump=None, timeout=300, done=None):
        """done 返回真值时视同已确认，例如检测到登录完成。"""
        with self.condition:
            self.report(message)
            self.answer = None
            self.state.update(status=kind, choices=choices or [], prompt=uuid.uuid4().hex)
        deadline = time.monotonic() + timeout
        checked = 0
        # 等人工输入时也须让同步驱动派发导航和网络事件。
        while True:
            with self.condition:
                if self.answer is not None or self.cancelled.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QueryError("等待人工确认超时，查询已停止。完成操作后可重新提交。")
                self.condition.wait(min(.15 if pump else remaining, remaining))
            if pump:
                pump()
            if done and time.monotonic() - checked >= 1.5:
                checked = time.monotonic()
                if done():
                    with self.condition:
                        if self.answer is None:
                            self.answer = True
                    break
        with self.condition:
            self.report("继续查询")
            self.state.update(status="running", choices=[], prompt=None)
            return self.answer

    def reply(self, prompt, answer):
        with self.condition:
            if not prompt or prompt != self.state.get("prompt") or self.answer is not None:
                raise Busy("此操作已过期，请查看当前查询状态。")
            if self.state["status"] == "choose":
                if answer not in [b["id"] for b in self.state["choices"]]:
                    raise ValueError("请选择列出的公司。")
            elif answer is not True:
                raise ValueError("请先完成 Chrome 窗口中的操作。")
            self.answer = answer
            self.state.update(status="running", prompt=None, message="正在确认")
            self.condition.notify_all()

    def cancel(self):
        with self.condition:
            if self.export_task and self.export_task.snapshot()["status"] == "running":
                self.export_task.cancel()
                return
            self.cancelled.set()
            self.condition.notify_all()

    def finish(self, status, message, **extra):
        with self.condition:
            if self.state.get("city_progress"):
                self.state["city_progress"] = [
                    dict(item, status="partial", message=message) if item["status"] == "running" else dict(item)
                    for item in self.state["city_progress"]]
            self.state.update(status=status, message=message, prompt=None, choices=[],
                              finished_at=time.time(), **extra)


class Service:
    def __init__(self, dev=False, directory=None, profile=None):
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boss")
        self.browser = Browser(dev=dev, profile=profile)
        custom_directory = directory is not None
        directory = Path(directory) if custom_directory else DATA / "dev" if dev else DATA
        samples = directory / "examples" if dev or custom_directory else ROOT / "examples"
        self.accounts = AccountProfiles(directory, self.browser.profile, samples)
        directory, profile, samples = self.accounts.paths(self.accounts.active)
        self.browser = Browser(profile=profile)
        self.store = Store(directory, samples=samples)
        self.boss = BossQuery(self.browser, self.store)
        self.tasks = {}
        self.lock = threading.RLock()
        self.closed = False
        self.active_export = None
        self.account_task = None

    def submit(self, data):
        params = validate(data)
        with self.lock:
            if self.closed:
                raise Busy("应用正在退出，请稍后重新打开。")
            if self.account_task and self.account_task.snapshot()['status'] not in ('done', 'partial', 'error'):
                raise Busy('请先完成或取消账号登录，再开始查询。')
            Guard(self.store, lambda message: None).check()
            if self.active_export and self.active_export.snapshot()["status"] == "running":
                raise Busy("正在补充导出所需的工商信息，请先完成或停止补充。")
            latest = next(reversed(self.tasks.values()), None)
            if latest and latest.snapshot()["status"] not in ("done", "partial", "error"):
                raise Busy("已有查询正在进行，请先完成或取消当前查询。")
            if len(self.tasks) >= 12:
                del self.tasks[next(iter(self.tasks))]
            task = Task(params)
            task.state.update(account_id=self.accounts.active, account_name=self.accounts.get(self.accounts.active)['name'])
            self.tasks[task.id] = task
            self.worker.submit(self.run, params, task)
            return task

    def access_status(self):
        with self.lock:
            return dict(Guard(self.store, lambda message: None).access_status(), accounts=self.account_status())

    def account_status(self):
        with self.lock:
            return dict(active=self.accounts.active, items=[dict(entry) for entry in self.accounts.entries],
                        operation=self.account_task.snapshot() if self.account_task else None)

    def open_login(self, data):
        """在当前查询会话打开官方认证页，不接收账号名称或凭据。"""
        if not isinstance(data, dict) or data:
            raise ValueError('请在 BOSS 官方窗口输入手机号和验证码。')
        with self.lock:
            return self.login_account({'account_id': self.accounts.active})

    def login_account(self, data):
        if not isinstance(data, dict) or (('name' in data) == ('account_id' in data)):
            raise ValueError('请选择已有账号，或填写新账号名称。')
        with self.lock:
            if self.closed:
                raise Busy('应用正在退出。')
            latest = next(reversed(self.tasks.values()), None)
            if latest and latest.snapshot()['status'] not in ('done', 'partial', 'error'):
                raise Busy('请先停止当前查询，再切换账号。')
            if self.account_task and self.account_task.snapshot()['status'] not in ('done', 'partial', 'error'):
                raise Busy('已有登录操作正在进行，请先完成或取消。')
            entry = self.accounts.add(data['name']) if 'name' in data else self.accounts.get(data['account_id'])
            task = Task({})
            task.state.update(account_id=entry['id'], account_name=entry['name'], message='正在打开专用登录窗口')
            self.account_task = task
            self.worker.submit(self.run_account_login, entry['id'], task)
            return self.account_status()

    def account_reply(self, kind, data):
        with self.lock:
            task = self.account_task
            if not task or data.get('id') != task.id:
                raise Busy('登录操作已过期，请刷新页面。')
            if kind == 'cancel':
                task.cancel()
            elif kind == 'reply':
                task.reply(data.get('prompt'), data.get('answer'))
            else:
                raise ValueError('登录操作无效。')
            return self.account_status()

    def run_account_login(self, ident, task):
        """用户主动打开登录入口；不打开岗位页、不启动查询、不清除暂停。"""
        try:
            if ident != self.accounts.active:
                self.browser.close()
                directory, profile, samples = self.accounts.paths(ident)
                with self.lock:
                    self.browser = Browser(profile=profile)
                    self.store = Store(directory, samples=samples)
                    self.boss = BossQuery(self.browser, self.store)
                    self.accounts.active = ident
                    self.accounts.save()
            task.sleep(0)
            with self.lock:
                self.accounts.get(ident)['ready'] = False
                self.accounts.save()
            page = self.browser.page()
            page.goto('https://www.zhipin.com/web/user/?ka=header-login', wait_until='domcontentloaded', timeout=20000)
            page.bring_to_front()

            def logged_in():
                from urllib.parse import urlsplit
                try:
                    check_page(page)
                except LoginRequired:
                    return False
                url = urlsplit(page.url)
                confirmed = (url.hostname in {'www.zhipin.com', 'zhipin.com'}
                             and not url.path.startswith(('/web/user', '/login')) and signed_in(page))
                if confirmed:
                    # 读取登录标记期间仍可能发生跳转，成功前再检查一次当前页。
                    check_page(page)
                return confirmed

            if not logged_in():
                task.ask('login', '请在 BOSS 官方 Chrome 窗口使用手机号／验证码或扫码登录。页面显示账号头像后会自动确认；日常 Chrome 的登录状态不会同步到此窗口。',
                         pump=lambda: page.wait_for_timeout(100), done=logged_in)
            if not logged_in():
                raise QueryError('尚未确认登录成功，请完成登录后重新打开登录入口。')
            task.sleep(0)
            with self.lock:
                self.accounts.get(ident)['ready'] = True
                self.accounts.save()
            access = Guard(self.store, task.report).access_status()
            task.finish('done', '已确认登录，未发起岗位查询。' + (access['message'] if access['blocked'] else '登录状态不代表岗位访问一定可用，可手动点击“查询岗位”。'))
        except RiskBlocked as exc:
            guard = Guard(self.store, task.report)
            guard.record_risk(exc.code, exc.until)
            task.finish('error', guard.access_status()['message'])
        except (QueryError, OSError, ValueError) as exc:
            task.finish('error', '已取消等待登录，可重新打开官方登录窗口。' if task.cancelled.is_set() else str(exc))
        except Exception as exc:
            if is_target_closed_error(exc):
                self.browser.close()
                task.finish('error', '登录窗口已关闭或连接中断，请重新打开 BOSS 官方登录窗口。')
            else:
                task.finish('error', f'登录窗口操作失败（{type(exc).__name__}），请重新打开登录入口。')

    def get(self, ident=None):
        with self.lock:
            return self.tasks.get(ident) if ident else next(reversed(self.tasks.values()), None)

    def prepare_export(self, ident):
        """兼容旧客户端的准备接口：仅确认已有结果，绝不启动采集。"""
        with self.lock:
            if self.closed:
                raise Busy("应用正在退出，请稍后重新打开。")
            task = self.tasks.get(ident)
            state = task.snapshot() if task else {}
            if state.get("status") not in ("done", "partial"):
                raise ValueError("查询结果尚未就绪。")
            export = Task({})
            missing = sum(not job.get("registered_capital") or not job.get("established_date")
                          for job in state["result"]["jobs"])
            export.finish("done", "直接导出已采集数据，未获取的工商字段留空；不访问 BOSS。",
                          missing_count=missing)
            with task.condition:
                task.export_task = export
            return task

    def run(self, params, task):
        try:
            result = self.boss.run(params, task)
            if params.get('company_details'):
                from sites.boss_details import CompanyDetailsCollector
                if task.cancelled.is_set() or result.get('stop_reason') == 'interrupted':
                    result['company_details'] = dict(complete=False, visited=0,
                        message='本次查询已中断，未继续访问详情页；已有岗位可直接导出。')
                else:
                    try:
                        task.set_phase('company_details')
                        result['company_details'] = CompanyDetailsCollector(self.browser, self.store).collect(result['jobs'], task)
                    except Exception:
                        # 补充阶段异常不得抹掉已经完成的岗位查询，也不自动重复访问。
                        result['company_details'] = dict(complete=False,
                            message='工商信息补充异常，已停止；已有岗位和已读取字段仍可直接导出。')
                result['stop_message'] = result.get('stop_message', '岗位查询完成') + ' ' + result['company_details']['message']
            partial = result.get("complete") is False
            partial = partial or result.get('company_details', {}).get('complete') is False
            task.finish("partial" if partial else "done",
                        result.get("stop_message", "查询完成"), result=result)
        except (QueryError, OSError, ValueError) as exc:
            task.finish("error", str(exc))
        except ModuleNotFoundError as exc:
            task.finish("error", f"缺少依赖 {exc.name}，请按 README 更新项目环境。")
        except Exception as exc:
            if is_target_closed_error(exc):
                self.browser.close()
                task.finish("error", "Chrome 窗口已关闭或连接中断，本次查询已停止。再次点击查询会自动打开浏览器。")
            else:
                # 不把浏览器日志中的请求参数或账号信息返回给客户端。
                task.finish("error", f"查询失败（{type(exc).__name__}）。请查看 Chrome 窗口并检查项目环境。")

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            task = next(reversed(self.tasks.values()), None)
            export = self.active_export
            account_task = self.account_task
        if account_task:
            account_task.cancel()
        if export:
            export.cancel()
        if task:
            task.cancel()
        try:
            self.worker.submit(self.browser.close).result()
        finally:
            self.worker.shutdown()
