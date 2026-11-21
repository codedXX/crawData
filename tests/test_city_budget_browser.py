"""省份单市批数预算：广州持续有更多结果时，仍须自然导航下一城市。"""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from guard import QueryError
from service import validate
from sites.boss_search import SearchSession
from store import Store
from test_search_browser import HTML


TARGETS = [("广州", "101280100"), ("深圳", "101280600")]
GUANGZHOU = 101280100
SHENZHEN = 101280600


@unittest.skipUnless(os.environ.get("BOSS_BROWSER_TESTS") == "1",
                     "设置 BOSS_BROWSER_TESTS=1 运行离线 Chrome 回放")
class CityBudgetBrowserTests(unittest.TestCase):
    def replay(self, *, per_city_batches=None, guangzhou_end=None, stall=None):
        from patchright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            browser = driver.chromium.launch(channel="chrome", headless=True)
            try:
                context = browser.new_context()
                requests, documents = [], []

                def posting(ident, city):
                    return dict(encryptJobId=ident, jobName="电商运营", salaryDesc="10-20K",
                                brandName="测试公司", encryptBrandId="brand", cityName=city, skills=[])

                def fulfill(route):
                    if route.request.resource_type == "document":
                        documents.append(parse_qs(urlsplit(route.request.url).query))
                        html = HTML
                        if '101280100' in route.request.url:
                            if stall == 'page':
                                html = html.replace('page:++page', 'page:1')
                            elif stall == 'no_request':
                                html = html.replace('list.addEventListener(\'scroll\'', 'list.addEventListener(\'unused\'')
                        route.fulfill(content_type="text/html", body=html)
                        return
                    if "/search/joblist.json" not in route.request.url:
                        route.fulfill(status=404, body="")
                        return
                    request = route.request.post_data_json
                    requests.append(request)
                    code, page_number = request["city"], request["page"]
                    if code == GUANGZHOU and stall == 'request_failed' and page_number == 2:
                        route.abort('failed')
                        return
                    city = "广州" if code == GUANGZHOU else "深圳"
                    ident = 1 if stall == 'repeat' and code == GUANGZHOU else len(requests)
                    if stall is None:
                        ident = page_number
                    jobs = [posting("shared", city), posting(f"{code}-{ident}", city)]
                    # 默认/限量用例中广州永远声明有下一批，只有客户端预算能触发切市。
                    more = code == GUANGZHOU and (guangzhou_end is None or page_number < guangzhou_end)
                    route.fulfill(content_type="application/json", body=json.dumps(
                        dict(code=0, zpData=dict(jobList=jobs, hasMore=more))))

                context.route("**/*", fulfill)
                task = Mock()

                def fail_fast_if_city_never_yields(message):
                    # 防止调度回归使测试本身无限滚动；服务器不借此宣告广州已读完。
                    if len(requests) > 12:
                        raise QueryError("离线回放发现单市持续占用，未切换城市")

                task.report.side_effect = fail_fast_if_city_never_yields
                page = context.new_page()
                session = SearchSession(page, Store(directory), task)
                page.on("request", session.meter)
                params = dict(keyword="运营", city="广东省", batches=0)
                if per_city_batches is not None:
                    params["per_city_batches"] = per_city_batches
                with patch("sites.boss_search.search_targets", return_value=TARGETS), \
                        patch("sites.boss_search.PROGRESS_TIMEOUT", 2 if stall == 'no_request' else 20):
                    result = session.read_search(validate(params))
                return result, requests, documents
            finally:
                browser.close()

    def assert_limited_province(self, result, requests, documents, expected_city_batches):
        self.assertFalse(result["complete"])
        self.assertEqual(result["stop_reason"], "city_limit")
        self.assertEqual((result["cities_visited"], result["cities_done"], result["cities_total"]), (2, 1, 2))
        self.assertEqual(result["limited_cities"], ["广州"])
        self.assertEqual(result["current_city"], "深圳")
        self.assertEqual(result["batches"], expected_city_batches + 1)
        self.assertEqual([request["city"] for request in requests],
                         [GUANGZHOU] * expected_city_batches + [SHENZHEN])
        self.assertEqual([request["page"] for request in requests],
                         list(range(1, expected_city_batches + 1)) + [1])
        self.assertEqual({request["query"] for request in requests}, {"运营"})
        self.assertEqual([query["city"] for query in documents], [[str(GUANGZHOU)], [str(SHENZHEN)]])
        self.assertEqual({query["query"][0] for query in documents}, {"运营"})
        self.assertEqual({job["url"].rsplit("/", 1)[-1] for job in result["jobs"]},
                         {"shared.html", f"{SHENZHEN}-1.html",
                          *(f"{GUANGZHOU}-{number}.html" for number in range(1, expected_city_batches + 1))})
        self.assertEqual(len(result["jobs"]), expected_city_batches + 2)
        self.assertEqual(result["summary"]["monthly_count"], expected_city_batches + 2)
        self.assertEqual(result["query"]["city"], "广东省")

    def test_optional_five_batches_yields_to_shenzhen_without_sixth_guangzhou_request(self):
        result, requests, documents = self.replay(per_city_batches=5)
        self.assert_limited_province(result, requests, documents, 5)

    def test_configured_two_batches_yields_to_shenzhen_without_third_guangzhou_request(self):
        result, requests, documents = self.replay(per_city_batches=2)
        self.assert_limited_province(result, requests, documents, 2)

    def test_zero_per_city_budget_waits_for_actual_end_after_seven_guangzhou_batches(self):
        result, requests, documents = self.replay(per_city_batches=0, guangzhou_end=7)
        self.assertTrue(result["complete"])
        self.assertEqual((result["cities_visited"], result["cities_done"], result["cities_total"]), (2, 2, 2))
        self.assertEqual(result["limited_cities"], [])
        self.assertEqual([request["city"] for request in requests], [GUANGZHOU] * 7 + [SHENZHEN])
        self.assertEqual([request["page"] for request in requests], list(range(1, 8)) + [1])
        self.assertEqual({request["query"] for request in requests}, {"运营"})
        self.assertEqual(len(documents), 2)
        self.assertEqual(result["batches"], 8)
        self.assertEqual(len(result["jobs"]), 9)

    def test_stalled_city_switches_in_real_browser_and_retains_partial_results(self):
        for stall, reason in [('repeat', 'no_new_jobs'), ('page', 'pagination_stalled'),
                              ('no_request', 'timeout'), ('request_failed', 'request_failed')]:
            with self.subTest(stall=stall):
                result, requests, documents = self.replay(per_city_batches=0, stall=stall)
                self.assertFalse(result['complete'])
                self.assertEqual(result['stop_reason'], 'city_error')
                self.assertEqual(result['failed_cities'][0]['city'], '广州')
                self.assertEqual(result['failed_cities'][0]['reason'], reason)
                self.assertEqual((result['cities_visited'], result['cities_done']), (2, 1))
                self.assertEqual(requests[-1]['city'], SHENZHEN)
                self.assertEqual(requests[-1]['page'], 1)
                self.assertEqual(len(documents), 2)
                self.assertTrue(any(job['city'] == '广州' for job in result['jobs']))
                self.assertTrue(any(job['city'] == '深圳' for job in result['jobs']))


if __name__ == "__main__":
    unittest.main()
