import time
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from guard import Guard, QueryError, RiskBlocked
from service import validate
from sites.boss import (SAMPLE, SchemaError, candidates, company_id, map_job,
                        matches, save_sample, unpack)
from store import Store, csv_bytes

# 明确构造的协议样例，不代表网站实测。
ITEM = dict(encryptJobId="job_1", encryptBrandId="brand_1", brandName="测试公司", jobName="Web 工程师",
            salaryDesc="15-25K·14薪", cityName="广州", skills=["Vue"], bossName="不保存")
BODY = dict(code=0, zpData=dict(jobList=[ITEM], hasMore=False))


class ReplayTests(unittest.TestCase):
    def test_search_and_mapping(self):
        data = unpack(BODY)
        brand, = candidates(data)
        self.assertEqual(brand, dict(id="brand_1", name="测试公司"))
        job = map_job(ITEM, brand)
        self.assertTrue(matches(job, "前端", "广州市"))
        self.assertFalse(matches(job, "Java", "广州"))
        self.assertFalse(matches(job, "Vue", "北京"))
        self.assertEqual((job["salary_min"], job["months"]), (15000, 14))
        self.assertNotIn("bossName", job)
        self.assertIn("Web 工程师", csv_bytes([job]).decode("utf-8-sig"))

    def test_replay_and_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "response.json"
            save_sample(unpack(BODY), path)
            text = path.read_text()
            self.assertNotIn("不保存", text)
            data = unpack(json.loads(text))
            self.assertEqual(map_job(data["jobList"][0], {"name": ""})["salary_raw"], "15-25K·14薪")

    def test_recorded_responses(self):
        guest = SAMPLE.with_name("boss_company_guest.json")
        samples = (SAMPLE, SAMPLE.with_name("boss_company_first_page.json"), SAMPLE.with_name("boss_company_querylist.json"))
        for path in [guest, *[p for p in samples if p.exists()]]:
            with self.subTest(path=path):
                data = unpack(json.loads(path.read_text()))
                jobs = [map_job(row, {"name": "公司页"}) for row in data["jobList"]]
                self.assertTrue(all(job["title"] and job["url"] for job in jobs))
                if path == guest:
                    self.assertEqual(len(jobs), 5)
                    self.assertTrue(all(j["salary_type"] == "unknown" and j["company"] == "爱来" for j in jobs))

    def test_company_document_state(self):
        from sites.boss_ssr import company_list
        # 与线上相同的参数复用、数组下标赋值和推荐列表，值使用虚构岗位。
        html = '''<script>window.__BOSSCOMPANY__=(function(a,b,c){
            a[0]={jobName:b,salaryDesc:"15-25K",encryptJobId:"one",brandName:"A",skills:["Vue"]};
            return {data:[{brandJobInfo:{jobList:a,hasMore:c},similarJobList:[{jobName:"无关岗位"}]}]};
        }(Array(1),"前端",false));</script>'''
        data = company_list(html)
        self.assertFalse(data["hasMore"])
        self.assertEqual([r["jobName"] for r in data["jobList"]], ["前端"])
        self.assertEqual(map_job(data["jobList"][0], {"name": "A"})["salary_min"], 15000)
        self.assertIsNone(company_list("<html></html>"))

    def test_bad_schemas_and_risk(self):
        for body in ({}, {"code": 0, "zpData": {}}, {"code": 0, "zpData": {"jobList": ["bad"]}}):
            with self.assertRaises(SchemaError):
                unpack(body)
        with self.assertRaises(RiskBlocked):
            unpack({"code": 37, "message": "访问异常"})
        with self.assertRaises(SchemaError):
            map_job(dict(ITEM, encryptJobId=""), {})
        with self.assertRaises(QueryError):
            company_id("https://www.zhipin.com.evil.test/gongsi/job/abc.html")

    def test_frontend_title_boundaries(self):
        excluded = ["Java Web 后端开发", "H5游戏运营", "Web3 研究员", "全栈工程师", "Java后端工程师", "Web3开发工程师", "webmaster开发"]
        included = ["前端开发", "Web开发工程师", "H5开发", "React 工程师", "Vue开发", "小程序开发"]
        for title in excluded + included:
            with self.subTest(title=title):
                job = dict(title=title, skills=["Vue", "React", "前端"], city="北京")
                self.assertEqual(matches(job, "前端", ""), title in included)
        self.assertTrue(matches(dict(title="全栈工程师", skills=["Vue"], city=""), "Vue", ""))

    def test_api_errors_do_not_all_become_risk(self):
        for code, message in [(400, "参数错误"), (500, "服务暂时不可用"), (37, "参数错误")]:
            with self.subTest(code=code):
                with self.assertRaises(QueryError) as caught:
                    unpack(dict(code=code, message=message))
                self.assertNotIsInstance(caught.exception, RiskBlocked)
        with self.assertRaises(RiskBlocked):
            unpack(dict(code=999, message="访问行为异常，请完成安全验证"))

    def test_collect_blocks_boss_direct_and_redirected_urls(self):
        from postings import SourceRedirect, read_source
        from urllib.request import Request
        for url in ("https://www.zhipin.com/", "https://zhipin.com/", "https://sub.ZHIPIN.com./"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "query"):
                    read_source(url)
                with self.assertRaisesRegex(ValueError, "query"):
                    SourceRedirect().redirect_request(Request("https://example.com"), None, 302, "", {}, url)

    def test_http_403_is_not_automatically_risk(self):
        from sites.boss import BossSession
        with self.assertRaises(QueryError) as caught:
            BossSession.check_status(Mock(status=403, ok=False))
        self.assertNotIsInstance(caught.exception, RiskBlocked)

    def test_validation(self):
        self.assertEqual(validate({"company": "公司"})["keyword"], "前端")
        self.assertEqual(validate({"company": "公司"})["pages"], 5)
        for pages in range(1, 6):
            self.assertEqual(validate({"company": "A", "pages": pages})["pages"], pages)
        for data in ({"company": ""}, {"company": "A", "pages": 0}, {"company": "A", "pages": 6}, {"company": "A", "pages": True}):
            with self.assertRaises(ValueError):
                validate(data)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.guard = Guard(self.store, lambda msg: None, lambda t: None)

    def test_daily_limits_and_lock(self):
        self.guard.start_company()
        self.guard.list_request()
        self.guard.finish_company(success=True)
        self.assertEqual(self.guard.record_risk(37), 1)
        self.assertEqual((self.guard.state()["company_limit"], self.guard.state()["request_limit"]), (10, 30))
        self.guard.clear_verification()
        self.assertEqual(self.guard.record_risk(37), 2)
        with self.assertRaisesRegex(QueryError, "明天"):
            self.guard.check()
        yesterday = dict(self.guard.state(), date="2001-01-01")
        self.store.write("quota.json", yesterday)
        self.assertEqual(self.guard.check()["risks"], 0)
        self.assertEqual(len((Path(self.temp.name) / "events.log").read_text().splitlines()), 2)

    def test_exhaustion_and_delay(self):
        state = self.guard.state()
        self.store.write("quota.json", dict(state, requests=60))
        with self.assertRaises(QueryError):
            self.guard.list_request()
        self.store.write("quota.json", dict(state, companies=20))
        with self.assertRaises(QueryError):
            self.guard.start_company()
        with patch("guard.time.time", return_value=time.time()):
            self.store.write("quota.json", dict(state, companies=5, last_end=time.time(), risks=1))
            self.guard.sleep = Mock()
            self.guard.start_company()
        seconds, = self.guard.sleep.call_args.args
        self.assertGreaterEqual(seconds, 400)
        self.assertLessEqual(seconds, 720)

    def test_failed_company_attempt_preserves_quota_and_cooldown(self):
        self.guard.start_company()
        self.guard.finish_company()
        self.assertEqual(self.guard.state()["companies"], 0)
        self.guard.sleep = Mock()
        self.guard.start_company()
        self.guard.sleep.assert_called_once()
        self.guard.finish_company(success=True)
        self.assertEqual(self.guard.state()["companies"], 1)

