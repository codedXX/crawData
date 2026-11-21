"""可选离线 Chrome 回放；所有请求由 route 本地响应，不访问 BOSS。"""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from service import validate
from sites.boss_search import SearchSession
from store import Store


HTML = '''<!doctype html><meta charset="utf-8"><style>
body { display:flex; gap:40px; height:1500px; }
ul { height:240px; width:400px; overflow-y:auto; margin:40px; padding:0; }
li { height:160px; list-style:none; } aside { margin-top:40px; }
</style><div class="job-list-container"><ul id="jobs" class="rec-job-list"></ul></div><aside><a href="/job_detail/detail.html">右侧详情链接</a></aside>
<script>
const list = document.getElementById('jobs');
let busy=false, more=true, page=0;
async function load() {
  if (busy || !more) return;
  busy=true;
  const response=await fetch('/wapi/zpgeek/search/joblist.json', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({query:new URLSearchParams(location.search).get('query'),city:Number(new URLSearchParams(location.search).get('city')),page:++page})
  });
  const {zpData:data}=await response.json();
  const rows=data.jobList.map(job=>{
    const li=document.createElement('li'), a=document.createElement('a');
    a.href='/job_detail/'+job.encryptJobId+'.html'; a.textContent=job.jobName;
    li.append(a); return li;
  });
  // 回收上一批 DOM，始终只留下本批卡片。
  list.replaceChildren(...rows); list.scrollTop=0;
  more=data.hasMore; busy=false;
}
list.addEventListener('scroll',()=>{
  if (list.scrollTop+list.clientHeight>=list.scrollHeight-10) load();
});
load();
</script>'''


@unittest.skipUnless(os.environ.get('BOSS_BROWSER_TESTS') == '1', '设置 BOSS_BROWSER_TESTS=1 运行离线 Chrome 回放')
class BrowserSearchTests(unittest.TestCase):
    def test_virtual_list_post_requests_and_right_detail_link(self):
        self.replay_search('深圳', ['深圳'], 7)

    def test_province_search_uses_city_requests_when_province_endpoint_is_empty(self):
        """重现截图：省码无结果，广州/深圳有运营岗位；不得拿省码的空响应判全省为空。"""
        from patchright.sync_api import sync_playwright
        targets = [('广州', '101280100'), ('深圳', '101280600'), ('珠海', '101280700')]
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                context = browser.new_context()
                seen, documents = [], []
                def posting(ident, city, company='common'):
                    return dict(encryptJobId=ident, jobName='电商运营', salaryDesc='10-20K',
                                brandName=company, encryptBrandId=company, cityName=city, skills=[])
                def fulfill(route):
                    if route.request.resource_type == 'document':
                        documents.append(parse_qs(urlsplit(route.request.url).query))
                        route.fulfill(content_type='text/html', body=HTML)
                        return
                    if '/search/joblist.json' not in route.request.url:
                        route.fulfill(status=404, body='')
                        return
                    request = route.request.post_data_json
                    seen.append(request)
                    code, page_number = request['city'], request['page']
                    if code == 101280100 and page_number == 1:
                        jobs = [posting('shared', '广州'), posting('gz', '广州', 'gz-company'),
                                posting('outside', '北京', 'outside-company')]
                        more = True
                    elif code == 101280100 and page_number == 2:
                        jobs, more = [posting('gz-second', '广州', 'gz-company')], False
                    elif code == 101280600:
                        jobs = [posting('shared', '深圳'), posting('sz', '深圳', 'sz-company')]
                        more = False
                    else:
                        # 不支持省码的接口与无岗位城市均返回空；旧实现会错误地在这里结束。
                        jobs, more = [], False
                    route.fulfill(content_type='application/json', body=json.dumps(
                        dict(code=0, zpData=dict(jobList=jobs, hasMore=more))))
                context.route('**/*', fulfill)
                page = context.new_page()
                session = SearchSession(page, Store(directory), Mock())
                page.on('request', session.meter)
                with patch('sites.boss_search.search_targets', return_value=targets):
                    result = session.read_search(validate(dict(keyword='运营', city='广东省', batches=0, per_city_batches=0)))
                self.assertTrue(result['complete'])
                self.assertEqual(result['batches'], 4)
                self.assertEqual((result['cities_total'], result['cities_done']), (3, 3))
                self.assertEqual(result['query']['city'], '广东省')
                self.assertEqual([request['city'] for request in seen],
                                 [101280100, 101280100, 101280600, 101280700])
                self.assertEqual([request['page'] for request in seen], [1, 2, 1, 1])
                self.assertEqual({request['query'] for request in seen}, {'运营'})
                self.assertEqual([query['city'] for query in documents],
                                 [['101280100'], ['101280600'], ['101280700']])
                self.assertNotIn(101280000, [request['city'] for request in seen])
                self.assertEqual({job['url'].rsplit('/', 1)[-1] for job in result['jobs']},
                                 {'shared.html', 'gz.html', 'gz-second.html', 'sz.html'})
                self.assertEqual(result['company_count'], 3)
                self.assertEqual({job['city'] for job in result['jobs']}, {'广州', '深圳'})
            finally:
                browser.close()

    def replay_search(self, region, cities, batches):
        from patchright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as driver:
            browser = driver.chromium.launch(channel='chrome', headless=True)
            try:
                context = browser.new_context()
                seen = []
                def fulfill(route):
                    if route.request.resource_type == 'document':
                        route.fulfill(content_type='text/html', body=HTML)
                        return
                    if '/search/joblist.json' not in route.request.url:
                        route.fulfill(status=404, body='')
                        return
                    request = route.request.post_data_json
                    seen.append(request)
                    page = request['page']
                    jobs = [dict(encryptJobId=f'job-{page}-{i}', jobName='前端开发', salaryDesc='10-20K',
                                 brandName=f'公司{i}', encryptBrandId=f'brand{i}', cityName=cities[i % len(cities)], skills=[])
                            for i in range(2)]
                    route.fulfill(content_type='application/json', body=json.dumps(
                        dict(code=0, zpData=dict(jobList=jobs, hasMore=page < batches))))
                context.route('**/*', fulfill)
                page = context.new_page()
                session = SearchSession(page, Store(directory), Mock())
                page.on('request', session.meter)
                result = session.read_search(validate(dict(keyword='前端', city=region, batches=0, per_city_batches=0)))
                self.assertTrue(result['complete'])
                self.assertEqual(result['batches'], batches)
                self.assertEqual(len(result['jobs']), batches * 2)
                self.assertEqual(result['company_count'], 2)
                self.assertEqual([request['page'] for request in seen], list(range(1, batches + 1)))
                self.assertEqual({request['city'] for request in seen}, {101280600})
                self.assertEqual({request['query'] for request in seen}, {'前端'})
                self.assertEqual({job['city'] for job in result['jobs']}, set(cities))
                self.assertEqual(page.locator('ul a').count(), 2)
            finally:
                browser.close()
