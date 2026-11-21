"""仅依据岗位名称与技能推测行业，结果不代表公司的工商登记行业。"""
import re
import unicodedata


UNKNOWN_INDUSTRY = "无法判断（岗位信息不足）"
ESTIMATE_SUFFIX = "（按岗位估算）"

# 优先使用明确的业务领域；财务、人事、销售等通用岗位不决定公司所属行业。
DOMAIN_RULES = (
    ("医药医疗相关行业", r"医生|医师|护士|护理|药师|药剂|临床|口腔|医学|医疗|医药|康复治疗"),
    ("教育培训", r"教师|老师|教研|教学|授课|学科辅导|课程顾问|教育培训|幼教|早教|讲师"),
    ("金融业", r"银行|保险|证券|期货|基金|信贷|投顾|投资顾问|理财顾问|金融风控"),
    ("房地产业", r"房地产|房产|置业|售楼|房屋经纪|租房"),
    ("交通运输、仓储和邮政业", r"物流|仓储|快递|货运|货代|报关|运输调度|仓库管理|配送员"),
    ("住宿和餐饮业", r"餐饮|厨师|后厨|烹饪|西点师|咖啡师|调酒师|酒店|客房|民宿"),
    ("制造业", r"机械|数控|\bcnc\b|模具|注塑|焊接|焊工|车间|机加工|生产操作|工厂|半导体工艺|电路设计|硬件工程|电气工程|电气自动化"),
)
SOFTWARE_TITLE = re.compile(
    r"前端|后端|软件开发|软件工程|软件测试|程序员|小程序开发|移动开发|算法工程|数据开发|"
    r"运维工程|测试开发|信息安全|网络安全|"
    r"\b(?:web|h5|react|vue|android|ios|java|python|golang|php|\.net)\b.*(?:开发|工程师|developer|engineer)",
    re.I,
)
SOFTWARE_SKILLS = re.compile(
    r"\b(?:vue(?:\.js)?|react(?:\.js)?|angular|javascript|typescript|node(?:\.js)?|"
    r"django|flask|spring(?:boot)?|golang|kubernetes)\b",
    re.I,
)


def normalized(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def estimate_industry(job):
    """使用岗位本身的线索；线索不足时明确返回无法判断，不参考公司名称。"""
    title = normalized(job.get("title") or "")
    skills = " ".join(normalized(skill) for skill in (job.get("skills") or []) if isinstance(skill, str))
    # 标题中的业务领域比开发技能更具体，例如“银行前端开发”的银行领域。
    for industry, pattern in DOMAIN_RULES:
        if re.search(pattern, title):
            return industry + ESTIMATE_SUFFIX
    if SOFTWARE_TITLE.search(title):
        return "软件和信息技术服务业" + ESTIMATE_SUFFIX
    for industry, pattern in DOMAIN_RULES:
        if re.search(pattern, skills):
            return industry + ESTIMATE_SUFFIX
    if SOFTWARE_SKILLS.search(skills):
        return "软件和信息技术服务业" + ESTIMATE_SUFFIX
    return UNKNOWN_INDUSTRY
