"""将招聘薪资统一为月薪（人民币元），保留原始口径。"""
import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP, localcontext
from statistics import mean, median

NUMBER = r"(\d+(?:\.\d+)?)"
UNIT = r"(万|千|k|元)?"
AMOUNT = re.compile(rf"{NUMBER}{UNIT}(?:[-~–—至]{NUMBER}{UNIT})?(?:元)?(以上|起)?(/月|/年|/天|/日|/小时|/时)?")
SCALE = {"万": 10000, "千": 1000, "k": 1000, "元": 1}


def parse_salary(raw):
    result = dict(min=None, max=None, months=None, type="unknown", raw=raw)
    if not isinstance(raw, str) or len(raw) > 128:
        return result
    value = "".join(unicodedata.normalize("NFKC", raw).lower().split())
    if value in {"面议", "薪资面议", "薪酬面议"}:
        return dict(result, type="negotiable")
    months = re.search(r"[·•×*](\d+)薪$", value)
    pay_months = int(months[1]) if months else 12
    if months:
        value = value[:months.start()]
    match = AMOUNT.fullmatch(value)
    if not match or pay_months <= 0:
        return result
    low, low_unit, high, high_unit, above, period = match.groups()
    unit = low_unit or high_unit
    if (not unit and not period) or (above and high):
        return result
    with localcontext() as context:
        context.prec = max(28, len(value) + 4)
        lo = Decimal(low) * SCALE.get(low_unit or unit, 1)
        hi = Decimal(high or low) * SCALE.get(high_unit or unit, 1)
        if not 0 < lo <= hi:
            return result
        if period in ("/天", "/日"):
            return dict(result, type="daily")
        if period in ("/小时", "/时"):
            return dict(result, type="hourly")
        if period == "/年":
            if months:
                return result
            lo, hi = lo / 12, hi / 12
        return dict(result, min=float(lo.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                    max=None if above else float(hi.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
                    months=pay_months, type="monthly")



def summarize(salaries):
    monthly = [s for s in salaries if s["type"] == "monthly" and s["min"] is not None and s["max"] is not None]
    return {
        "monthly_count": len(monthly),
        "min": min((s["min"] for s in monthly), default=None),
        "max": max((s["max"] for s in monthly), default=None),
        "median": median((s["min"] + s["max"]) / 2 for s in monthly) if monthly else None,
        "avg_months": mean(s["months"] for s in monthly) if monthly else None,
    }
