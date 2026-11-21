"""从公司页可见的工商信息区读取注册资本和成立日期，不执行页面脚本。"""
import re
import unicodedata
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit


EMPTY_INFO = {"registered_capital": "", "established_date": ""}
LABELS = {"注册资本": "registered_capital", "注册资金": "registered_capital",
          "成立日期": "established_date", "成立时间": "established_date",
          "公司名称": "company_name", "企业名称": "company_name"}
LABEL_PATTERN = "|".join(LABELS)
SKIP_TAGS = {"script", "style", "template", "noscript", "svg"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
UNRELATED = re.compile(r"(?:recommend|related|similar|other-company|guess)", re.I)


class _Node:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def text(self):
        return "".join(item.text() if isinstance(item, _Node) else item for item in self.children)

    def walk(self):
        yield self
        for item in self.children:
            if isinstance(item, _Node):
                yield from item.walk()

    def excluded(self):
        current = self
        while current:
            attrs = current.attrs
            style = re.sub(r"\s+", "", attrs.get("style", "").lower())
            if (current.tag in SKIP_TAGS | {"aside", "nav"} or "hidden" in attrs
                    or attrs.get("aria-hidden") == "true" or "display:none" in style
                    or "visibility:hidden" in style
                    or UNRELATED.search(attrs.get("class", "") + " " + attrs.get("id", ""))):
                return True
            current = current.parent
        return False


class _Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(_Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        current = self.current
        while current.parent:
            if current.tag == tag:
                self.current = current.parent
                return
            current = current.parent

    def handle_data(self, value):
        self.current.children.append(value)


def _compact(value):
    return re.sub(r"\s+", "", value).rstrip(":：")


def _visible_text(node):
    if node.excluded():
        return ""
    return "".join(_visible_text(item) if isinstance(item, _Node) else item for item in node.children)


def _value(field, raw):
    value = re.sub(r"\s+", " ", raw).strip(" \t\r\n:：")
    if not value or len(value) > 150:
        return ""
    if field == "company_name":
        return value if not any(label in value for label in LABELS) else ""
    if field == "established_date":
        match = re.fullmatch(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", value)
        if not match:
            return ""
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return ""
    # 资本保留页面单位/币种；拒绝标签、整段工商文本或误配的日期。
    if any(label in value for label in (*LABELS, "实缴资本", "经营状态", "法定代表人", "企业名称", "企业类型")):
        return ""
    if not re.search(r"\d", value):
        return ""
    if re.search(r"元|万|亿|人民币|美元|港币|欧元|[¥￥$€]", value) or re.fullmatch(r"[\d,.]+", value):
        return value
    return ""


def _after_label(node, section):
    current = node
    while current is not section and current.parent:
        siblings = current.parent.children
        after = siblings[siblings.index(current) + 1:]
        if after:
            text = "".join(_visible_text(item) if isinstance(item, _Node) else item for item in after)
            if text.strip():
                yield text
            # dt/dd、th/td 的首个值不能拼上后续其他字段。
            first = next((item for item in after if isinstance(item, _Node) and not item.excluded()), None)
            if first is not None and (current.tag, first.tag) in {("dt", "dd"), ("th", "td")}:
                yield _visible_text(first)
        current = current.parent


def _section_info(section, include_identity=False):
    values = {field: set() for field in EMPTY_INFO}
    if include_identity:
        values['company_name'] = set()
    for node in section.walk():
        if node.excluded():
            continue
        text = _visible_text(node)
        label = _compact(text)
        if label in LABELS:
            field = LABELS[label]
            if field not in values:
                continue
            for raw in _after_label(node, section):
                value = _value(field, raw)
                if value:
                    values[field].add(value)
                    break
        else:
            inline = re.fullmatch(rf"\s*({LABEL_PATTERN})\s*[:：]\s*(.+?)\s*", text, re.S)
            if inline:
                field = LABELS[inline[1]]
                if field not in values:
                    continue
                value = _value(field, inline[2])
                if value:
                    values[field].add(value)
    return {field: next(iter(items)) if len(items) == 1 else "" for field, items in values.items()}


def _business_sections(document):
    sections = []
    for heading in document.root.walk():
        if heading.excluded() or heading.tag == "a" or _compact(_visible_text(heading)) != "工商信息":
            continue
        # 标题内部 span 与标题自身只处理一次；导航链接不算工商信息标题。
        if heading.parent and _compact(_visible_text(heading.parent)) == "工商信息":
            continue
        if any(item.tag == "a" for item in heading.walk()):
            continue
        current = heading.parent
        while current and current.tag not in {"document", "html", "body"}:
            if any(_compact(_visible_text(node)) in LABELS
                   or re.match(rf"\s*(?:{LABEL_PATTERN})\s*[:：]", _visible_text(node))
                   for node in current.walk() if not node.excluded()):
                if current not in sections:
                    sections.append(current)
                break
            current = current.parent
    return sections


def parse_company_info(html, include_identity=False):
    """仅配对工商信息标题所在区块中的精确标签，缺失或歧义值留空。"""
    document = _Document()
    document.feed(html)
    results = [_section_info(section, include_identity) for section in _business_sections(document)]
    if not results or any(result != results[0] for result in results[1:]):
        return dict(EMPTY_INFO, **({'company_name': ''} if include_identity else {}))
    return results[0]


def matches_detail_company(html, display_name, legal_name, company_id):
    """优先核对当前岗位的公司链接 ID，品牌名不必包含在工商全称中。

    只使用招聘公司区域或工商名称自身的链接，不以推荐公司、介绍正文中的
    任意品牌提及作为归属依据。无链接可核对时保留原有名称匹配兜底。
    """
    def normalized(value):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()

    display, legal = normalized(display_name), normalized(legal_name)
    if not legal:
        return False
    document = _Document()
    document.feed(html)
    business_nodes = {node for section in _business_sections(document) for node in section.walk()}
    employer_classes = {'job-banner', 'job-primary', 'job-top', 'sider-company',
                        'job-company', 'job-company-info', 'company-info', 'company-name'}
    identities = set()
    for node in document.root.walk():
        if node.tag != 'a' or node.excluded():
            continue
        text = normalized(_visible_text(node))
        in_employer = False
        current = node
        while current:
            if employer_classes.intersection(current.attrs.get('class', '').split()):
                in_employer = True
                break
            current = current.parent
        if not ((in_employer and text in {display, legal})
                or (node in business_nodes and text == legal)):
            continue
        href = node.attrs.get('href') or ''
        try:
            url = urlsplit(href)
        except ValueError:
            continue
        if (url.scheme not in {'', 'http', 'https'}
                or (url.netloc and url.hostname not in {'www.zhipin.com', 'zhipin.com'})
                or (not url.netloc and not href.startswith('/'))):
            continue
        match = re.fullmatch(r'/gongsi/(?:job/)?([A-Za-z0-9_~-]+)\.html/?', url.path)
        if match:
            identities.add(match[1])
    if company_id and identities:
        # 明确的 ID 冲突不能被相似名称覆盖。
        return identities == {company_id}
    return len(display) >= 2 and display in legal
