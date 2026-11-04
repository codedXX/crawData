"""按公司或跨公司搜索 BOSS 在招岗位，或离线读取 JobPosting。"""
import argparse
from contextlib import contextmanager, ExitStack
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from guard import QueryError
from app_lock import AlreadyRunning, instance_lock
from service import Busy, Service
from store import DATA, ROOT, csv_bytes


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_body(self, status, kind, body, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send_body(status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode())

    def local(self):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in hosts and (not origin or origin in {"http://" + h for h in hosts})

    def do_GET(self):
        if not self.local():
            return self.send_error(403)
        path = urlsplit(self.path).path
        if path == "/":
            return self.send_body(200, "text/html; charset=utf-8", (ROOT / "index.html").read_bytes())
        parts = path.strip("/").split("/")
        if parts[0] == "jobs" and len(parts) <= 3:
            task = self.server.service.get(parts[1] if len(parts) > 1 else None)
            if len(parts) == 1 or task:
                state = task.snapshot() if task else dict(status="idle", message="等待查询")
                if len(parts) < 3:
                    return self.send_json(200, state)
                if parts[2] == "csv" and state["status"] in ("done", "partial"):
                    return self.send_body(200, "text/csv; charset=utf-8",
                                          csv_bytes(state["result"]["jobs"]), "boss-jobs.csv")
        self.send_json(404, {"error": "查询不存在或结果尚未就绪。"})

    def do_POST(self):
        if not self.local():
            return self.send_error(403)
        if self.headers.get_content_type() != "application/json":
            return self.send_json(415, {"error": "需要 JSON 请求。"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 8192:
                raise ValueError("请求大小无效。")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("参数格式无效。")
            parts = self.path.strip("/").split("/")
            if parts == ["jobs", "query"]:
                task = self.server.service.submit(data)
                return self.send_json(202, task.snapshot())
            if len(parts) == 3 and parts[0] == "jobs":
                task = self.server.service.get(parts[1])
                if task and parts[2] in ("reply", "cancel"):
                    if parts[2] == "reply":
                        task.reply(data.get("prompt"), data.get("answer"))
                    else:
                        task.cancel()
                    return self.send_json(200, task.snapshot())
            self.send_json(404, {"error": "查询接口不存在，请刷新页面。"})
        except Busy as exc:
            self.send_json(409, {"error": str(exc)})
        except (QueryError, ValueError) as exc:
            self.send_json(400, {"error": str(exc)})


@contextmanager
def backend(port, attach, dev=False):
    """持有进程锁的服务独占 profile 和数据；其他 CLI 通过回环 HTTP 复用它。"""
    directory = DATA / "dev" if dev else DATA
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        try:
            stack.enter_context(instance_lock(directory / "service.guard"))
        except AlreadyRunning:
            # 创建者先拿锁后发布端口，短暂等待发布完成。
            for _ in range(30):
                try:
                    active = (directory / "service.lock").read_text(encoding="ascii").strip()
                except FileNotFoundError:
                    active = ""
                if active.isdigit():
                    break
                time.sleep(.1)
            else:
                raise QueryError("已有查询服务正在启动，请稍后重试。")
            url = f"http://127.0.0.1:{int(active)}"
            if not attach:
                raise QueryError(f"本项目的服务已启动，请打开 {url}/")
            yield url
            return
        lock = stack.enter_context((directory / "service.lock").open("w", encoding="ascii"))
        try:
            with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
                server.service = Service(dev=dev)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                lock.write(str(server.server_port))
                lock.flush()
                try:
                    yield f"http://127.0.0.1:{server.server_port}"
                finally:
                    server.shutdown()
                    thread.join()
                    server.service.close()
        finally:
            lock.seek(0)
            lock.truncate()


def request(url, data=None):
    req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                  headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=8) as response:
            return json.load(response)
    except HTTPError as exc:
        raise QueryError(json.load(exc)["error"]) from exc


def print_result(result, ident, directory=DATA):
    jobs, summary = result["jobs"], result["summary"]
    fmt = lambda n: "—" if n is None else f"{n:,.2f}".rstrip("0").rstrip(".")
    print(f"匹配 {len(jobs)} 个岗位，月薪样本 {summary['monthly_count']} 个")
    if result.get("mode") == "search":
        print(f"已读 {result['batches']} 批，匹配 {result['company_count']} 家公司")
        print(result["stop_message"])
        for company in result["companies"]:
            print(f"{company['company']}：{company['count']} 个岗位，中位月薪 {fmt(company['median'])} 元")
    if result.get("filter_mode") == "local":
        print("未找到站内筛选框，仅在已读取的公司岗位页面中匹配关键词。")
    for label, field in (("最低月薪（元）", "min"), ("最高月薪（元）", "max"),
                         ("岗位中值的中位数（元）", "median"), ("平均年薪月数", "avg_months")):
        print(label, fmt(summary[field]))
    from store import COLUMNS
    print("\t".join(COLUMNS.values()))
    for job in jobs:
        values = ["、".join(v) if isinstance(v, list) else str(v) if v is not None else "—"
                  for v in (job[field] for field in COLUMNS)]
        print("\t".join(values))
    path = directory / f"boss-{ident}.csv"
    path.write_bytes(csv_bytes(jobs))
    print(f"CSV 已写入 {path}")


def query(url, args):
    params = {k: getattr(args, k) for k in ("company", "keyword", "city", "pages", "batches")}
    params["reselect"] = args.reselect
    state = request(url + "/jobs/query", params)
    path = url + "/jobs/" + state["id"]
    last = None
    try:
        while state["status"] not in ("done", "partial", "error"):
            if state["message"] != last:
                last = state["message"]
                print(last, flush=True)
            if state["status"] == "choose":
                choices = state["choices"]
                for i, brand in enumerate(choices, 1):
                    print(f"{i}. {brand['name']} ({brand['id']})")
                entry = input("输入公司序号：").strip()
                if entry.isdecimal() and 1 <= int(entry) <= len(choices):
                    request(path + "/reply", dict(prompt=state["prompt"], answer=choices[int(entry)-1]["id"]))
            elif state["status"] in ("login", "verify"):
                input("完成后按回车：")
                request(path + "/reply", dict(prompt=state["prompt"], answer=True))
            else:
                time.sleep(.4)
            state = request(path)
    except (EOFError, KeyboardInterrupt):
        request(path + "/cancel", {})
        # 等工作线程发布已收集结果，避免临时服务随 CLI 退出而丢掉部分结果。
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = request(path)
            if state["status"] in ("done", "partial", "error"):
                break
            time.sleep(.2)
        else:
            raise QueryError("已请求停止，工作线程仍在结束当前操作。") from None
    if state["status"] == "error":
        raise QueryError(state["message"])
    print(state["message"])
    print_result(state["result"], state["id"], DATA / "dev" if args.dev else DATA)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="启动本地网页")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--dev", action="store_true", help="使用独立的开发数据和浏览器配置")
    search = commands.add_parser("query", help="查询岗位并导出 CSV；省略公司时跨公司搜索")
    search.add_argument("--company", default="", help="公司名称或 BOSS 公司链接；省略时跨公司搜索")
    search.add_argument("--dev", action="store_true", help="使用独立的开发数据和浏览器配置")
    search.add_argument("--reselect", action="store_true", help="重新选择公司并更新名称映射")
    search.add_argument("--keyword", default="前端")
    search.add_argument("--city", default="")
    search.add_argument("--pages", default=5, type=int, choices=(1, 2, 3, 4, 5), help="公司查询读取页数")
    search.add_argument("--batches", default=0, type=int, help="跨公司搜索批数，0 自动加载到结束（默认，无每日次数上限）；可选 1–60")
    scrape = commands.add_parser("collect", help="读取 HTML 中的 JobPosting 并输出 JSON")
    scrape.add_argument("source")
    scrape.add_argument("--company", default="")
    scrape.add_argument("--keyword", default="前端")
    args = cli.parse_args()
    try:
        if args.command == "collect":
            from postings import collect, read_source
            html, _ = read_source(args.source)
            print(json.dumps(collect(html, args.company, args.keyword), ensure_ascii=False))
        elif args.command == "query":
            with backend(0, attach=True, dev=args.dev) as url:
                query(url, args)
        else:
            dev = getattr(args, "dev", False)
            port = getattr(args, "port", None) or (8766 if dev else 8765)
            with backend(port, attach=False, dev=dev) as url:
                print(f"岗位查询页面 {url}/", flush=True)
                threading.Event().wait()
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, QueryError) as exc:
        sys.exit(f"失败：{exc}")


if __name__ == "__main__":
    main()
