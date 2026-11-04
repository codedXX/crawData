"""用事件驱动的假页面覆盖查询入口的回归，所有写入均使用临时目录。"""
import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import Mock, patch

from guard import QueryError
from service import Service, Task, validate
from sites.boss import BossQuery, BossSession, FILTER, HOME, NEXT, SchemaError
from store import Store

BRAND = dict(id="brand", name="测试公司")
SOURCE = HOME + "gongsi/job/brand.html"
COMPANY_LIST = HOME + "wapi/zpgeek/brand/job/querylist.json"


def job(title, ident, salary="15-25K"):
    return dict(jobName=title, encryptJobId=ident, encryptBrandId="brand", brandName="测试公司",
                salaryDesc=salary, skills=["Vue"], cityName="北京")


class Response:
    def __init__(self, data, document=False, url=None):
        self.url = url or (SOURCE if document else HOME + "wapi/zpgeek/search/joblist.json")
        self.request = Mock(url=self.url, resource_type="document" if document else "xhr")
        self.status, self.ok, self.data = 200, True, data

    def json(self):
        return self.data

    def text(self):
        state = json.dumps({"data": [{"brandJobInfo": self.data["zpData"]}]})
        return '<script>window.__BOSSCOMPANY__=(function(){return ' + state + ';}());</script>'


class Control:
    def __init__(self, page, kind):
        self.page, self.kind = page, kind
        self.first = self.last = self

    def is_visible(self):
        return (self.kind == "next" and self.page.has_next) or (self.kind == "search" and self.page.has_filter)

    def count(self):
        return int(self.kind == "avatar" and self.page.authenticated)

    def is_enabled(self):
        return True

    def locator(self, selector):
        return self

    def get_attribute(self, name):
        return ""

    def press(self, key):
        if key == "Enter":
            self.page.emit_next()

    def press_sequentially(self, text, delay):
        self.page.typed.append((text, delay))

    def click(self, **kwargs):
        self.page.clicks += 1
        self.page.emit_next()

    def hover(self, **kwargs):
        self.page.hovered = True
        if self.page.load_on_hover:
            self.page.emit_next()


class Page:
    def __init__(self, pages, has_filter=True, list_url=None):
        self.url = HOME
        self.handlers = defaultdict(list)
        self.pages = iter(pages)
        self.has_filter = has_filter
        self.list_url = list_url
        self.has_next = True
        self.hovered = False
        self.load_on_hover = False
        self.authenticated = True
        self.typed = []
        self.clicks = 0
        self.bootstrap = dict(code=0, zpData=dict(jobList=[job("后端工程师", "bootstrap", "100-200K")], hasMore=False))
        self.mouse = Mock()

    def on(self, event, callback):
        self.handlers[event].append(callback)

    def remove_listener(self, event, callback):
        self.handlers[event].remove(callback)

    def emit(self, data, document=False):
        response = Response(data, document, None if document else self.list_url)
        for callback in list(self.handlers["request"]):
            callback(response.request)
        for callback in list(self.handlers["response"]):
            callback(response)

    def emit_next(self):
        self.emit(next(self.pages))

    def goto(self, url, **kwargs):
        self.url = url
        self.emit(self.bootstrap, document=True)

    def wait_for_timeout(self, milliseconds):
        pass

    def locator(self, selector):
        kind = {FILTER: "search", NEXT: "next"}.get(selector, "avatar" if "img.avatar" in selector else "other")
        return Control(self, kind)

    def get_by_text(self, text, exact=False):
        return Control(self, "job")

    def is_closed(self):
        return False

    def bring_to_front(self):
        pass


class BossFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.params = validate(dict(company=SOURCE, keyword="前端", pages=1))
        self.task = Mock()
        self.browser = Mock()
        self.query = BossQuery(self.browser, self.store)

    def run_query(self, responses, list_url=None, **params):
        self.page = Page(responses, list_url=list_url)
        self.browser.page.return_value = self.page
        return self.query.run(dict(self.params, **params), self.task)

    def test_one_page_means_first_filtered_page(self):
        body = dict(code=0, zpData=dict(jobList=[job("前端工程师", "one"), job("H5游戏运营", "bad")], hasMore=True))
        result = self.run_query([body])
        self.assertEqual([j["title"] for j in result["jobs"]], ["前端工程师"])
        self.assertEqual(result["pages"], 1)
        self.assertEqual(result["summary"]["max"], 25000)
        self.assertEqual(result["filter_mode"], "site")
        self.assertEqual([t for t, _ in self.page.typed], ["前", "端"])
        self.assertEqual(self.store.read("quota.json")["companies"], 1)
        self.assertEqual(self.store.read("quota.json")["requests"], 2)
        self.assertFalse(any(self.page.handlers.values()))

    def test_live_input_response_needs_no_duplicate_submit(self):
        body = dict(code=0, zpData=dict(jobList=[job("前端工程师", "one")], hasMore=False))
        original = Control.press_sequentially
        def type_and_search(control, text, delay):
            original(control, text, delay)
            if text == "端":
                control.page.emit_next()
        with patch.object(Control, "press_sequentially", type_and_search):
            result = self.run_query([body])
        self.assertEqual(result["pages"], 1)
        self.assertEqual(self.store.read("quota.json")["requests"], 2)

    def test_two_filtered_pages(self):
        pages = [dict(code=0, zpData=dict(jobList=[job("前端", "one")], hasMore=True)),
                 dict(code=0, zpData=dict(jobList=[job("Web开发", "two")], hasMore=False))]
        result = self.run_query(pages, pages=2)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual(self.page.clicks, 1)
        self.assertEqual(self.store.read("quota.json")["requests"], 3)

    def test_company_querylist_filter_and_pagination(self):
        # 公司职位页筛选/翻页实际使用 querylist.json，不是搜索页的 joblist.json。
        pages = [dict(code=0, zpData=dict(jobList=[job("销售", "one", "10-12K")], hasMore=True)),
                 dict(code=0, zpData=dict(jobList=[job("销售经理", "two", "7-12K")], hasMore=True)),
                 dict(code=0, zpData=dict(jobList=[job("销售主管", "three", "12-15K")], hasMore=True)),
                 dict(code=0, zpData=dict(jobList=[job("销售顾问", "four", "10-14K")], hasMore=True)),
                 dict(code=0, zpData=dict(jobList=[job("销售总监", "five", "20-30K")], hasMore=True))]
        # 第五页仍有更多岗位，也必须按上限停止，不请求第六页。
        result = self.run_query(pages, list_url=COMPANY_LIST + "?query=test&page=1", keyword="销售", pages=5)
        self.assertEqual(result["pages"], 5)
        self.assertEqual(result["filter_mode"], "site")
        self.assertEqual(len(result["jobs"]), 5)
        self.assertEqual((result["summary"]["min"], result["summary"]["max"], result["summary"]["median"]),
                         (7000, 30000, 12000))
        self.assertEqual(self.page.clicks, 4)
        self.assertEqual(self.store.read("quota.json")["requests"], 6)
        self.assertEqual(self.store.read("quota.json")["companies"], 1)
        self.assertFalse(any(self.page.handlers.values()))

    def test_repeated_query_reads_fresh_results_and_counts_each_request(self):
        self.store.remember("公司别名", BRAND)
        for salary, maximum in (("10-12K", 12000), ("15-25K", 25000)):
            body = dict(code=0, zpData=dict(jobList=[job("前端", "one", salary)], hasMore=False))
            result = self.run_query([body], company="公司别名", list_url=COMPANY_LIST)
            self.assertEqual(result["jobs"][0]["salary_raw"], salary)
            self.assertEqual(result["summary"]["max"], maximum)
            self.assertNotIn("cached", result)
            self.assertFalse((self.store.directory / "cache.json").exists())
        self.assertEqual(self.browser.page.call_count, 2)
        state = self.store.read("quota.json")
        self.assertEqual((state["companies"], state["requests"]), (2, 4))
        self.store.write("quota.json", dict(state, request_limit=4))
        result = self.run_query([body], company="公司别名", list_url=COMPANY_LIST)
        self.assertEqual(result["summary"]["max"], 25000)
        self.assertEqual(self.browser.page.call_count, 3)
        self.assertEqual(self.store.read("quota.json")["requests"], 6)

    def test_timeout_reports_stage_without_private_request_values(self):
        page = Page([], list_url=HOME + "wapi/zpgeek/brand/unknown.json?query=private-value")
        session = BossSession(page, self.store, self.task)
        with patch("sites.boss.time.monotonic", side_effect=[0, 16]):
            with self.assertRaisesRegex(SchemaError, "筛选结果第 1/1 页"):
                session.collect("筛选结果第 1/1 页", lambda: page.emit({}))
        diagnostic = self.store.read("last_failure.json")
        self.assertEqual(diagnostic["stage"], "筛选结果第 1/1 页")
        self.assertEqual(diagnostic["requests"], [dict(path="/wapi/zpgeek/brand/unknown.json", type="xhr", status=200)])
        self.assertNotIn("private-value", json.dumps(diagnostic))
        self.assertFalse(any(page.handlers.values()))

    def test_infinite_list_scrolls_over_job_card(self):
        pages = [dict(code=0, zpData=dict(jobList=[job("销售", "one")], hasMore=True)),
                 dict(code=0, zpData=dict(jobList=[job("销售经理", "two")], hasMore=False))]
        page = Page(pages, list_url=COMPANY_LIST)
        page.has_next = False
        def wheel(x, y):
            # 指针不在岗位卡片时，真实页面只滚动详情区，不会加载列表。
            self.assertTrue(page.hovered)
            page.emit_next()
        page.mouse.wheel.side_effect = wheel
        self.browser.page.return_value = page
        result = self.query.run(dict(self.params, keyword="销售", pages=5), self.task)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(len(result["jobs"]), 2)
        self.assertEqual(self.store.read("quota.json")["requests"], 3)
        page.mouse.wheel.assert_called_once()

    def test_hover_that_loads_next_page_does_not_scroll_again(self):
        pages = [dict(code=0, zpData=dict(jobList=[job("销售", "one")], hasMore=True)),
                 dict(code=0, zpData=dict(jobList=[job("销售经理", "two")], hasMore=False))]
        page = Page(pages, list_url=COMPANY_LIST)
        page.has_next = False
        page.load_on_hover = True
        self.browser.page.return_value = page
        result = self.query.run(dict(self.params, keyword="销售", pages=5), self.task)
        self.assertEqual(result["pages"], 2)
        page.mouse.wheel.assert_not_called()

    def test_second_page_failure_does_not_charge_company_or_risk(self):
        pages = [dict(code=0, zpData=dict(jobList=[job("前端", "one")], hasMore=True)),
                 dict(code=400, message="参数错误")]
        with self.assertRaisesRegex(QueryError, "code=400"):
            self.run_query(pages, pages=2)
        state = self.store.read("quota.json")
        self.assertEqual((state["companies"], state["requests"], state["risks"]), (0, 3, 0))
        self.assertFalse(self.store.read("cache.json"))
        self.assertFalse(any(self.page.handlers.values()))

    def test_legacy_exhausted_budget_allows_company_query_and_filter(self):
        body = dict(code=0, zpData=dict(jobList=[job("前端", "one")], hasMore=False))
        from guard import Guard
        state = Guard(self.store, Mock()).state()
        self.store.write("quota.json", dict(state, requests=30, request_limit=30,
                                             companies=20, company_limit=20))
        self.assertEqual(len(self.run_query([body], list_url=COMPANY_LIST, keyword="Vue")["jobs"]), 1)
        self.assertTrue(self.page.typed)
        state = self.store.read("quota.json")
        self.assertEqual((state["requests"], state["companies"]), (32, 21))

    def test_old_response_is_not_used_for_new_filter(self):
        page = Page([])
        stale = Response(dict(code=0, zpData=dict(jobList=[job("后端", "old")], hasMore=False)))
        session = BossSession(page, self.store, self.task)
        def action():
            for handler in list(page.handlers["response"]):
                handler(stale)
            page.emit(dict(code=0, zpData=dict(jobList=[job("前端", "new")], hasMore=False)))
        data = session.collect("筛选", action)
        self.assertEqual(data["jobList"][0]["encryptJobId"], "new")

    def test_login_stops_after_three_confirmations(self):
        page = Page([])
        page.authenticated = False
        self.browser.page.return_value = page
        with self.assertRaisesRegex(QueryError, "连续 3 次"):
            self.query.run(self.params, self.task)
        self.assertEqual(self.task.ask.call_count, 3)
        self.assertFalse(self.store.read("quota.json"))

    def test_prompt_finishes_when_login_detected(self):
        checks = iter([False, True])
        answer = Task({}).ask("login", "登录", pump=lambda: None, timeout=10, done=lambda: next(checks))
        self.assertIs(answer, True)

    def test_login_opens_login_link_once(self):
        page = Page([])
        page.authenticated = False
        link = Mock()
        link.first = link
        link.count.return_value = 1
        session = BossSession(page, self.store, self.task)
        with patch("sites.boss.login_link", return_value=link):
            with self.assertRaisesRegex(QueryError, "连续 3 次"):
                session.login()
        link.click.assert_called_once()
        self.assertEqual(self.task.ask.call_count, 3)

    def test_token_redirect_is_not_challenge(self):
        from guard import challenged
        page = Page([])
        page.url = HOME + "web/passport/zp/security.html?seed=x&name=y"
        def settle(predicate, timeout):
            page.url = HOME + "web/geek/jobs"
        page.wait_for_url = settle
        self.assertFalse(challenged(page))
        stuck = Page([])
        stuck.url = HOME + "web/passport/zp/security.html?seed=x"
        stuck.wait_for_url = Mock(side_effect=TimeoutError())
        self.assertTrue(challenged(stuck))

    def test_prompt_times_out(self):
        with self.assertRaisesRegex(QueryError, "超时"):
            Task({}).ask("login", "登录", timeout=.01)

    def test_reselect_updates_mapping(self):
        self.store.remember("别名", dict(id="old", name="选错的公司"))
        self.browser.page.return_value = Page([])
        with patch.object(BossSession, "login"), patch.object(BossSession, "collect", return_value={"jobList": [job("前端", "one")]}), patch.object(BossSession, "read", return_value={"jobs": []}):
            self.task.ask.return_value = "brand"
            self.query.run(dict(self.params, company="别名", reselect=True), self.task)
        self.assertEqual(self.store.company("别名"), BRAND)

    def test_dev_profile_and_data_are_separate(self):
        with patch("service.DATA", Path(self.tmp.name)):
            prod, dev = Service(), Service(dev=True)
            try:
                self.assertNotEqual(prod.browser.profile, dev.browser.profile)
                dev.boss.store.write("quota.json", {"requests": 30})
                self.assertFalse(prod.boss.store.read("quota.json"))
                self.assertEqual(dev.boss.store.samples, Path(self.tmp.name) / "dev" / "examples")
            finally:
                prod.close()
                dev.close()
