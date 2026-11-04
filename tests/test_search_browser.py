"""可选离线 Chrome 回放；所有请求由 route 本地响应，不访问 BOSS。"""
import json
import os
import tempfile
import unittest
from unittest.mock import Mock

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
    body:JSON.stringify({query:'前端',city:101280600,page:++page})
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
                                 brandName=f'公司{i}', encryptBrandId=f'brand{i}', cityName='深圳', skills=[])
                            for i in range(2)]
                    route.fulfill(content_type='application/json', body=json.dumps(
                        dict(code=0, zpData=dict(jobList=jobs, hasMore=page < 7))))
                context.route('**/*', fulfill)
                page = context.new_page()
                session = SearchSession(page, Store(directory), Mock())
                page.on('request', session.meter)
                result = session.read_search(validate(dict(keyword='前端', city='深圳')))
                self.assertTrue(result['complete'])
                self.assertEqual(result['batches'], 7)
                self.assertEqual(len(result['jobs']), 14)
                self.assertEqual(result['company_count'], 2)
                self.assertEqual([request['page'] for request in seen], list(range(1, 8)))
                self.assertEqual(page.locator('ul a').count(), 2)
            finally:
                browser.close()
