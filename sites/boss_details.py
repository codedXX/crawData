"""查询阶段读取岗位详情页工商信息；导出路径不调用本模块。"""
import re
from urllib.parse import urlsplit

from browser import is_target_closed_error
from guard import Guard, LoginRequired, QueryError, RiskBlocked, check_page, login_link
from sites.boss import BossSession, HOME, ID
from sites.boss_company import matches_detail_company, parse_company_info

FIELDS = ('registered_capital', 'established_date')
DETAIL_PATH = re.compile(rf'/job_detail/({ID.pattern})\.html')


def detail_url(job):
    value = job.get('url')
    if not isinstance(value, str):
        return None
    url = urlsplit(value)
    if (url.scheme != 'https' or url.netloc not in {'www.zhipin.com', 'zhipin.com'}
            or not DETAIL_PATH.fullmatch(url.path)):
        return None
    return HOME.rstrip('/') + url.path


class CompanyDetailsCollector:
    def __init__(self, browser, store):
        self.browser, self.store = browser, store

    def collect(self, jobs, task):
        from patchright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

        groups = {}
        for row in jobs:
            ident = row.get('company_id')
            identity = ident if isinstance(ident, str) and ID.fullmatch(ident) else row.get('url')
            groups.setdefault((identity, row.get('company', '')), []).append(row)
        guard = Guard(self.store, task.report, task.sleep)
        guard.report = lambda message: task.report(f'{guard.action} · {message}')
        page = session = None
        visited, checked = 0, 0
        issues = []
        interrupted = ''
        task.set_company_details_progress(checked, len(groups))

        def observe_response(response):
            url = urlsplit(response.url)
            if url.hostname in {'www.zhipin.com', 'zhipin.com'}:
                session.observe_risk(response)

        try:
            for (_, company), rows in groups.items():
                task.sleep(0)
                if session and session.access_error:
                    raise session.access_error
                # 已有同公司字段可复用；冲突值不相互覆盖。
                for field in FIELDS:
                    values = {row[field] for row in rows if row.get(field)}
                    if len(values) == 1:
                        value = next(iter(values))
                        for row in rows:
                            if not row.get(field):
                                row[field] = value
                if all(all(row.get(field) for field in FIELDS) for row in rows):
                    checked += 1
                    task.set_company_details_progress(checked, len(groups))
                    continue
                source_row = next((row for row in rows if detail_url(row)), None)
                if source_row is None:
                    issues.append(dict(company=company, message='没有可核对的岗位详情链接，工商字段留空。'))
                    checked += 1
                    task.set_company_details_progress(checked, len(groups))
                    continue
                source = detail_url(source_row)
                guard.check()
                guard.action = f'读取工商信息 {checked + 1}/{len(groups)} 家 · {company}'
                task.report(guard.action)
                guard.wait(guard.interval(15, 30))
                guard.check()
                task.sleep(0)
                if page is None:
                    page = self.browser.page().context.new_page()
                    session = BossSession(page, self.store, task)
                    page.on('request', session.meter)
                    page.on('response', observe_response)
                session.guard.action = guard.action
                session.before_action()
                visited += 1
                response = page.goto(source, wait_until='domcontentloaded', timeout=20000)
                check_page(page)
                if session.access_error:
                    raise session.access_error
                if response is not None:
                    session.check_status(response)
                if login_link(page).count():
                    raise LoginRequired('工商信息读取需要重新登录，已停止；已有岗位仍可导出。')
                current = urlsplit(page.url)
                if (current.hostname not in {'www.zhipin.com', 'zhipin.com'}
                        or current.path != urlsplit(source).path):
                    raise QueryError('岗位详情页发生跳转，无法核对公司，已停止补充工商信息。')
                try:
                    page.get_by_text('工商信息', exact=True).first.wait_for(state='visible', timeout=5000)
                except BrowserTimeout:
                    pass
                check_page(page)
                if session.access_error:
                    raise session.access_error
                task.sleep(0)
                html = page.content()
                info = parse_company_info(html, include_identity=True)
                if session.access_error:
                    raise session.access_error
                if not matches_detail_company(html, company, info.get('company_name', ''), source_row.get('company_id')):
                    issues.append(dict(company=company, message='未确认工商主体与招聘公司一致，相关字段留空。'))
                else:
                    for row in rows:
                        for field in FIELDS:
                            if not row.get(field) and info.get(field):
                                row[field] = info[field]
                    if not all(info.get(field) for field in FIELDS):
                        issues.append(dict(company=company, message='详情页未提供完整工商字段，缺失项留空。'))
                checked += 1
                task.set_company_details_progress(checked, len(groups))
                task.report(f'已检查工商信息 {checked}/{len(groups)} 家公司')
        except RiskBlocked as exc:
            guard.record_risk(exc.code, exc.until)
            interrupted = guard.access_status()['message']
        except (QueryError, OSError, ValueError) as exc:
            interrupted = str(exc)
        except BrowserError as exc:
            if session and isinstance(session.access_error, RiskBlocked):
                error = session.access_error
                guard.record_risk(error.code, error.until)
                interrupted = guard.access_status()['message']
            elif is_target_closed_error(exc):
                interrupted = '工商信息窗口已关闭，已停止补充。'
            else:
                # 再检查导航异常是否落到了真正的受限页面。
                try:
                    check_page(page)
                except RiskBlocked as risk:
                    guard.record_risk(risk.code, risk.until)
                    interrupted = guard.access_status()['message']
                except (QueryError, BrowserError):
                    pass
                interrupted = interrupted or '岗位详情页加载失败，已停止补充工商信息。'
        finally:
            if page is not None:
                try:
                    page.remove_listener('request', session.meter)
                    page.remove_listener('response', observe_response)
                    page.close()
                except BrowserError:
                    pass
            if visited:
                guard.finish_company(success=False)
        missing = sum(not all(row.get(field) for field in FIELDS) for row in jobs)
        message = f'工商信息已检查 {checked}/{len(groups)} 家公司，访问 {visited} 个岗位详情页。'
        if missing:
            message += f'{missing} 个岗位仍缺少注册资本或成立日期，缺失项留空。'
        mismatched = sum('工商主体' in item['message'] for item in issues)
        if mismatched:
            message += f'其中 {mismatched} 家公司无法核对工商主体，未填入未经确认的信息。'
        if interrupted:
            message += interrupted + ' 已保留岗位和已读取的工商信息，可直接导出。'
        return dict(complete=not interrupted and not missing, visited=visited, checked=checked,
                    total=len(groups), missing_count=missing, issues=issues, message=message)
