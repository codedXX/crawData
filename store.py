"""由单个查询工作线程管理的本地 JSON 文件和 CSV 导出。"""
import csv
import io
import json
import os
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def key(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as f:
            name = f.name
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        # Windows 不允许替换仍被打开的临时文件。
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


class Store:
    def __init__(self, directory=DATA, samples=None):
        self.directory = Path(directory)
        self.samples = Path(samples) if samples is not None else self.directory / "samples"

    def read(self, name):
        try:
            data = json.loads((self.directory / name).read_text(encoding="utf-8"))
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
    "company": "公司", "title": "岗位", "salary_raw": "薪资原文", "salary_min": "最低月薪（元）",
    "salary_max": "最高月薪（元）", "months": "年薪月数", "salary_type": "薪资类型",
    "city": "城市", "area": "区域", "experience": "经验", "degree": "学历",
    "skills": "技能", "url": "岗位链接",
}


def csv_bytes(jobs):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(COLUMNS.values())
    for job in jobs:
        row = []
        for field in COLUMNS:
            value = job[field]
            if isinstance(value, list):
                value = "、".join(value)
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            row.append(value)
        writer.writerow(row)
    return stream.getvalue().encode("utf-8-sig")
