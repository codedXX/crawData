"""BOSS 地区代码与省份归属，供站内搜索和公司岗位筛选共用。"""
import json
from pathlib import Path


# 城市快照来自官方公开字典；省级代码于 2026-11-08 核对同一字典的 cityList。
# https://www.zhipin.com/wapi/zpCommon/data/city.json
CITIES = json.loads(Path(__file__).with_name("boss_cities.json").read_text(encoding="utf-8"))
PROVINCES = {
    "北京": "101010000", "上海": "101020000", "天津": "101030000", "重庆": "101040000",
    "黑龙江": "101050000", "吉林": "101060000", "辽宁": "101070000", "内蒙古": "101080000",
    "河北": "101090000", "山西": "101100000", "陕西": "101110000", "山东": "101120000",
    "新疆": "101130000", "西藏": "101140000", "青海": "101150000", "甘肃": "101160000",
    "宁夏": "101170000", "河南": "101180000", "江苏": "101190000", "湖北": "101200000",
    "浙江": "101210000", "安徽": "101220000", "福建": "101230000", "江西": "101240000",
    "湖南": "101250000", "贵州": "101260000", "四川": "101270000", "广东": "101280000",
    "云南": "101290000", "广西": "101300000", "海南": "101310000", "香港": "101320000",
    "澳门": "101330000", "台湾": "101340000",
}
UNRESTRICTED = {"", "全国", "不限", "不限城市", "不限省份", "不限地区"}
PROVINCE_SUFFIXES = ("壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "特别行政区", "省")


def location_options():
    """级联选择与搜索计划共用同一份地区字典。"""
    special = {"内蒙古": "内蒙古自治区", "广西": "广西壮族自治区",
               "西藏": "西藏自治区", "宁夏": "宁夏回族自治区", "新疆": "新疆维吾尔自治区"}
    direct = {"北京", "上海", "天津", "重庆", "香港", "澳门"}
    options = []
    for name in PROVINCES:
        value = name if name in direct else special.get(name, name + "省")
        label = name + "市" if name in {"北京", "上海", "天津", "重庆"} else value
        options.append(dict(label=label, value=value, direct=name in direct,
                            cities=[dict(label=city, value=city, code=code)
                                    for city, code in search_targets(value)]))
    return options


def city_code(city):
    """保留 city 参数，兼容省份全称/简称及已有城市输入。"""
    name = city.strip()
    if name in UNRESTRICTED:
        return CITIES["全国"]
    # 吉林默认沿用吉林市；“吉林省”才表示全省。直辖市继续使用原城市代码。
    code = CITIES.get(name) or CITIES.get(name.removesuffix("市"))
    if code:
        return code
    for suffix in PROVINCE_SUFFIXES:
        if name.endswith(suffix):
            name = name.removesuffix(suffix)
            break
    code = PROVINCES.get(name)
    if not code:
        raise ValueError("未识别省份或城市，请填写省份或完整城市名（例如广东省、深圳市），或留空搜索全国。")
    return code


def location_matches(city, location):
    """按官方代码层级判断岗位城市归属，不猜测缺失或未知的城市。"""
    target = city_code(location)
    if target == CITIES["全国"]:
        return True
    name = city.strip()
    actual = CITIES.get(name) or CITIES.get(name.removesuffix("市"))
    if not actual or actual == CITIES["全国"]:
        return False
    if target.endswith("0000"):
        return actual[:5] == target[:5]
    return actual == target


def search_targets(location):
    """省份用于规划城市搜索；站点搜索始终只接收城市代码或全国代码。"""
    code = city_code(location)
    if code.endswith("0000") and code != CITIES["全国"]:
        targets = [(name, value) for name, value in CITIES.items()
                   if value[:5] == code[:5] and not value.endswith("0000")]
        if not targets:
            raise ValueError("该省份缺少可查询的城市，请更新地区字典。")
        return targets
    name = next((name for name, value in CITIES.items() if value == code), location.strip())
    return [(name, code)]


def selected_cities(location, selection):
    """校验省内多选，按地区字典顺序返回去重后的城市名。"""
    code = city_code(location)
    if not code.endswith("0000") or code == CITIES["全国"]:
        raise ValueError("多选城市时请先选择所属省份。")
    if not isinstance(selection, list) or not 1 <= len(selection) <= len(CITIES):
        raise ValueError("请至少选择一个城市，或选择全省。")
    targets = search_targets(location)
    allowed = {value for name, value in targets}
    codes = set()
    for name in selection:
        if not isinstance(name, str) or len(name) > 80:
            raise ValueError("所选城市格式无效。")
        value = city_code(name)
        if value not in allowed:
            raise ValueError("所选城市必须属于当前省份，请重新选择。")
        codes.add(value)
    return [name for name, value in targets if value in codes]
