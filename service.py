"""网页和 CLI 共用的单线程查询队列与人工确认流程。"""
import time
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from browser import Browser, is_target_closed_error
from guard import QueryError
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
            raise ValueError("公司、关键词或城市格式无效。")
        params[name] = value.strip()
    if not params["company"] and not params["keyword"]:
        raise ValueError("跨公司搜索请填写岗位关键词。")
    company_id(params["company"])
    if not params["company"]:
        from sites.boss_search import city_code
        city_code(params["city"])
    pages = data.get("pages", 5)
    if type(pages) is not int or not 1 <= pages <= 5:
        raise ValueError("每次读取 1 至 5 页。")
    reselect = data.get("reselect", False)
    if type(reselect) is not bool:
        raise ValueError("重新选择公司参数必须为布尔值。")
    if reselect and not params["company"]:
        raise ValueError("跨公司搜索不能重新选择同名公司。")
    batches = data.get("batches", 0)
    if type(batches) is not int or not 0 <= batches <= 60:
        raise ValueError("搜索批数应为 0（自动加载）或 1 至 60。")
    return dict(params, pages=pages, batches=batches, reselect=reselect)


class Task:
    def __init__(self, params):
        self.id = uuid.uuid4().hex
        self.condition = threading.Condition()
        self.cancelled = threading.Event()
        self.answer = None
        self.state = dict(id=self.id, query=params, status="running", message="准备查询")

    def snapshot(self):
        with self.condition:
            return self.state.copy()

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
            self.cancelled.set()
            self.condition.notify_all()

    def finish(self, status, message, **extra):
        with self.condition:
            self.state.update(status=status, message=message, prompt=None, choices=[], **extra)


class Service:
    def __init__(self, dev=False):
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="boss")
        self.browser = Browser(dev=dev)
        directory = DATA / "dev" if dev else DATA
        samples = directory / "examples" if dev else ROOT / "examples"
        self.boss = BossQuery(self.browser, Store(directory, samples=samples))
        self.tasks = {}
        self.lock = threading.Lock()

    def submit(self, data):
        params = validate(data)
        with self.lock:
            latest = next(reversed(self.tasks.values()), None)
            if latest and latest.snapshot()["status"] not in ("done", "partial", "error"):
                raise Busy("已有查询正在进行，请先完成或取消当前查询。")
            if len(self.tasks) >= 12:
                del self.tasks[next(iter(self.tasks))]
            task = Task(params)
            self.tasks[task.id] = task
            self.worker.submit(self.run, params, task)
            return task

    def get(self, ident=None):
        with self.lock:
            return self.tasks.get(ident) if ident else next(reversed(self.tasks.values()), None)

    def run(self, params, task):
        try:
            result = self.boss.run(params, task)
            partial = result.get("complete") is False
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
        task = self.get()
        if task:
            task.cancel()
        try:
            self.worker.submit(self.browser.close).result()
        finally:
            self.worker.shutdown()
