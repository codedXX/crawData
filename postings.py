"""原有 collect 命令使用的 JobPosting (JSON-LD) 读取器。"""
import json
import re
import unicodedata
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

def company_key(name):
    return unicodedata.normalize("NFKC", name).strip().casefold()


class PostingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_json = False
        self.blocks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and (dict(attrs).get("type") or "").strip().lower() == "application/ld+json":
            self.in_json = True
            self.blocks.append("")

    def handle_data(self, data):
        if self.in_json:
            self.blocks[-1] += data

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_json = False


def postings(value):
    if isinstance(value, list):
        for item in value:
            yield from postings(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if "JobPosting" in types:
            yield value
        else:
            for child in value.values():
                yield from postings(child)


def check_source_url(url):
    host = (urlsplit(url).hostname or "").rstrip(".").lower()
    if host == "zhipin.com" or host.endswith(".zhipin.com"):
        raise ValueError("BOSS 链接请使用 query 命令或本地查询页面。")


class SourceRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_source_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_source(source):
    if urlsplit(source).scheme in ("http", "https"):
        check_source_url(source)
        req = Request(source, headers={"User-Agent": "JobTracker/0.1"})
        with build_opener(SourceRedirect()).open(req, timeout=20) as response:
            encoding = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(encoding), response.geturl()
    path = Path(source).resolve()
    return path.read_text(encoding="utf-8-sig"), path.as_uri()


def salary_fields(salary):
    if isinstance(salary, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-~–至]\s*(\d+(?:\.\d+)?)\s*[kK]\s*(?:[·•×*]\s*(\d+)\s*薪)?\s*", salary)
        if match:
            lo, hi, months = match.groups()
            return salary, float(lo) * 1000, float(hi) * 1000, "MONTH", None, months
        return salary, None, None, None, None, None
    if isinstance(salary, dict):
        value = salary.get("value")
        if not isinstance(value, dict):
            value = {"value": value}
        return (json.dumps(salary, ensure_ascii=False), value.get("minValue", value.get("value")),
                value.get("maxValue", value.get("value")), value.get("unitText"),
                salary.get("currency"), None)
    return (None,) * 6


def posted_date(value):
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date().isoformat()
        except ValueError:
            pass
    return None


def collect(html, company_filter, keyword):
    parser = PostingParser()
    parser.feed(html)
    jobs = []
    for block in parser.blocks:
        try:
            jobs.extend(postings(json.loads(block)))
        except json.JSONDecodeError:
            continue
    if not jobs:
        raise ValueError("页面里没有可解析的 JobPosting 数据。请检查是否为职位详情页，或是否需要为此来源添加解析器。")

    rows = []
    for job in jobs:
        org = job.get("hiringOrganization") or {}
        company = org.get("name") if isinstance(org, dict) else org
        title = job.get("title")
        if not isinstance(company, str) or not isinstance(title, str):
            raise ValueError("职位缺少公司名称或岗位名称")
        if company_filter and company_key(company_filter) != company_key(company):
            continue
        if keyword and keyword.casefold() not in title.casefold():
            continue
        raw, low, high, unit, currency, months = salary_fields(job.get("baseSalary"))
        rows.append(dict(company=company, title=title, posted_on=posted_date(job.get("datePosted")),
                         salary_raw=raw, salary_min=low, salary_max=high, salary_unit=unit,
                         currency=currency, pay_months=months))
    return rows


