"""网页和桌面版共享的页面数据。"""
import json

from sites.boss_locations import location_options
from store import ROOT


def page_html():
    catalog = json.dumps(location_options(), ensure_ascii=False).replace("<", "\\u003c")
    return ((ROOT / "index.html").read_text(encoding="utf-8")
            .replace("/*LOCATION_OPTIONS*/[]", catalog)
            .replace("/*MULTI_CITY_SUPPORTED*/false", "true"))
