"""由单个查询工作线程管理的本地 JSON 文件和 CSV 导出。"""
import csv
import io
import json
import os
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path

from industry import estimate_industry

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
# 界面轮询和采集线程会访问同一文件；Windows 的普通读取句柄不共享删除权限。
# 所有 Store 实例和 write_json 调用共用锁，避免读取期间执行原子替换。
_JSON_LOCK = threading.RLock()
_REPLACE_DELAYS = (.05, .1, .2, .4, .8, .8)


def key(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def write_json(path, value):
    with _JSON_LOCK:
        _write_json(Path(path), value)


def _replace_json(source, target):
    # 只重试本地替换；不重新序列化、不重发网站请求。外部扫描程序的短暂占用
    # 不受进程内锁控制，持续权限错误仍须停止，不能退回非原子的覆盖写入。
    for delay in (*_REPLACE_DELAYS, None):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if sys.platform != 'win32' or getattr(exc, 'winerror', None) not in {5, 32, 33}:
                raise
            if delay is None:
                raise PermissionError(
                    f'无法保存本地状态（{target.name}）：文件持续被占用或没有写入权限。'
                    '原有记录未被覆盖，请关闭占用该文件的软件并检查数据目录写入权限后重试。'
                ) from exc
            time.sleep(delay)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as f:
            name = f.name
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        # Windows 不允许替换仍被打开的临时文件。
        _replace_json(name, path)
    finally:
        if name is not None:
            try:
                Path(name).unlink(missing_ok=True)
            except OSError:
                # 外部占用也可能阻止清理，不能掩盖真正的保存异常。
                pass


class Store:
    def __init__(self, directory=DATA, samples=None):
        self.directory = Path(directory)
        self.samples = Path(samples) if samples is not None else self.directory / "samples"

    def read(self, name):
        try:
            with _JSON_LOCK:
                text = (self.directory / name).read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("应为 JSON 对象")
            return data
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise ValueError(f"{name} 损坏，请检查该文件。") from exc

    def write(self, name, value):
        write_json(self.directory / name, value)

    def company(self, name):
        return self.read("companies.json").get(key(name))

    def remember(self, name, brand):
        companies = self.read("companies.json")
        companies[key(name)] = brand
        self.write("companies.json", companies)


COLUMNS = {
    "registered_capital": "注册资金", "industry": "所属行业", "established_date": "成立日期",
    "company": "公司", "title": "岗位", "salary_raw": "薪资原文", "salary_min": "最低月薪（元）",
    "salary_max": "最高月薪（元）", "months": "年薪月数", "salary_type": "薪资类型",
    "city": "城市", "area": "区域", "experience": "经验", "degree": "学历",
    "skills": "技能", "url": "岗位链接",
}


def export_value(job, field):
    if field == "industry":
        return job.get(field) or estimate_industry(job)
    if field in {"registered_capital", "established_date"}:
        return job.get(field, "")
    return job[field]


def csv_bytes(jobs):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(COLUMNS.values())
    for job in jobs:
        row = []
        for field in COLUMNS:
            value = export_value(job, field)
            if isinstance(value, list):
                value = "、".join(value)
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            row.append(value)
        writer.writerow(row)
    return stream.getvalue().encode("utf-8-sig")
