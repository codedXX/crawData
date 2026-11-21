"""工商字段离线结构样例，不代表真实页面已登录验收。"""
import unittest

from sites.boss_company import matches_detail_company, parse_company_info


class CompanyInfoTests(unittest.TestCase):
    def test_legal_company_link_confirms_different_brand_name(self):
        html = '''<section><h3>工商信息</h3><ul>
          <li><span>公司名称</span><a href="https://www.zhipin.com/gongsi/brand.html?ka=detail">星财智科技（深圳）有限公司</a></li>
          <li><span>成立日期</span><span>2020-12-21</span></li>
          <li><span>注册资金</span><span>1900万美元</span></li></ul></section>'''
        info = parse_company_info(html, include_identity=True)
        self.assertEqual(info['registered_capital'], '1900万美元')
        self.assertTrue(matches_detail_company(html, '星财富', info['company_name'], 'brand'))
        self.assertFalse(matches_detail_company(html, '星财富', info['company_name'], 'other'))

    def test_recommended_hidden_external_and_description_links_are_not_identity_evidence(self):
        link = '<a href="/gongsi/brand.html">星财富</a>'
        for content in (
            '<div class="recommend-companies"><div class="company-info">' + link + '</div></div>',
            '<div class="company-info" hidden>' + link + '</div>',
            '<div class="company-info"><a href="https://example.com/gongsi/brand.html">星财富</a></div>',
            '<section><h3>公司介绍</h3><p>合作伙伴：' + link + '</p></section>',
        ):
            with self.subTest(content=content):
                self.assertFalse(matches_detail_company(content, '星财富', '星财智科技（深圳）有限公司', 'brand'))

    def test_company_id_conflict_overrides_name_match_and_ambiguous_links(self):
        html = '<div class="job-banner"><a href="/gongsi/wrong.html">测试科技</a></div>'
        self.assertFalse(matches_detail_company(html, '测试科技', '深圳测试科技有限公司', 'brand'))
        html += '<div class="company-info"><a href="/gongsi/brand.html">测试科技</a></div>'
        self.assertFalse(matches_detail_company(html, '测试科技', '深圳测试科技有限公司', 'brand'))
        self.assertTrue(matches_detail_company('', '测试科技', '深圳测试科技有限公司', 'brand'))
        self.assertFalse(matches_detail_company(html, '测试科技', '', 'brand'))

    def test_desktop_business_panel_uses_exact_fields(self):
        html = '''<main><section><h3>工商信息</h3><div class="business-detail"><ul>
          <li><span>企业名称：</span><span>示例公司</span></li>
          <li><span>成立时间：</span><span>2021-08-30</span></li>
          <li><span>经营状态：</span><span>存续</span></li>
          <li><span>实缴资本：</span><span>500万人民币</span></li>
          <li><span>注册资本：</span><span>1000万人民币</span></li>
        </ul></div></section></main>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="1000万人民币", established_date="2021-08-30"))

    def test_mobile_definition_list_and_date_normalization(self):
        html = '''<section><h3>工商信息</h3><dl>
          <dt>注册资本：</dt><dd>200.5万美元</dd>
          <dt>成立日期：</dt><dd>2020年2月29日</dd>
          <dt>经营状态：</dt><dd>存续</dd></dl></section>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="200.5万美元", established_date="2020-02-29"))

    def test_nested_flex_columns(self):
        html = '''<section><h3><span>工商信息</span></h3>
          <div><div><span>注册资本：</span></div><div>1000万人民币</div></div>
          <div><div><span>成立日期：</span></div><div>2021/8/30</div></div>
          </section>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="1000万人民币", established_date="2021-08-30"))

    def test_inline_labels_and_table_cells(self):
        html = '''<section><h3>工商信息</h3><p>注册资本：1000万人民币</p>
          <table><tr><th>成立日期：</th><td>2021-8-30</td><th>实缴资本：</th><td>1万人民币</td></tr></table></section>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="1000万人民币", established_date="2021-08-30"))

    def test_other_dates_and_paid_in_capital_are_not_substituted(self):
        html = '''<section><h3>工商信息</h3><ul>
          <li><span>实缴资本：</span><span>600万元</span></li>
          <li><span>核准日期：</span><span>2024-01-01</span></li>
          <li><span>营业期限：</span><span>2021-08-30 至无固定期限</span></li>
          </ul></section>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="", established_date=""))

    def test_recommendations_and_unrelated_fields_are_ignored(self):
        html = '''<main><section><h3>工商信息</h3><p>成立日期：2021-08-30</p></section></main>
          <aside><h3>工商信息</h3><p>注册资本：999亿元</p></aside>
          <div class="recommend-companies"><h3>工商信息</h3><p>注册资本：888亿元</p></div>
          <footer><span>注册资本：</span><span>777亿元</span></footer>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="", established_date="2021-08-30"))

    def test_no_business_heading_means_no_information(self):
        html = '''<div><h3>公司简介</h3><p>注册资本：1000万元</p><p>成立日期：2021-08-30</p></div>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="", established_date=""))

    def test_hidden_content_scripts_and_navigation_are_not_read(self):
        html = '''<nav><a href="#business">工商信息</a></nav><section><h3>工商信息</h3>
          <script>const data = '注册资本：999亿元 成立日期：1990-01-01';</script>
          <div hidden>注册资本：999亿元</div><p style="display: none">成立日期：1990-01-01</p>
          <p>注册资本：1000万人民币</p><p>成立日期：2021-08-30</p></section>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="1000万人民币", established_date="2021-08-30"))

    def test_invalid_date_and_missing_capital_stay_empty(self):
        html = '''<section><h3>工商信息</h3><p>注册资本：暂无</p><p>成立日期：2021-02-29</p></section>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="", established_date=""))

    def test_ambiguous_business_sections_do_not_mix_companies(self):
        html = '''<section><h3>工商信息</h3><p>注册资本：1000万元</p><p>成立日期：2021-08-30</p></section>
          <section><h3>工商信息</h3><p>注册资本：3000万元</p><p>成立日期：2022-08-30</p></section>'''
        self.assertEqual(parse_company_info(html), dict(registered_capital="", established_date=""))


if __name__ == "__main__":
    unittest.main()
