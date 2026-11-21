"""旧导出接口的本地兼容层：只整理已有岗位，绝不补查公司页面。"""
from industry import estimate_industry


class CompanyExporter:
    def __init__(self, browser, store):
        # 保留旧调用签名，但不持有浏览器或存储对象。
        pass

    def enrich(self, jobs, task):
        rows = []
        for job in jobs:
            row = dict(job)
            row.setdefault("registered_capital", "")
            row.setdefault("established_date", "")
            row["industry"] = row.get("industry") or estimate_industry(row)
            rows.append(row)
        missing = sum(not row["registered_capital"] or not row["established_date"] for row in rows)
        return dict(jobs=rows, complete=True, companies=0, missing_count=missing,
                    message="直接导出已采集数据，不访问 BOSS；未获取的工商字段留空，缺失行业仅按岗位本地估算。")
