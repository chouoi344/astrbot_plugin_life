"""Chinese calendar metadata; unknown years fall back to the weekday rule."""

from __future__ import annotations

from datetime import date

from astrbot.api import logger

from .errors import describe_error
from .wardrobe import season_for_day

_warned_days = set()


def get_calendar_info(day: date) -> dict:
    result = {
        "season": season_for_day(day),
        "is_workday": day.weekday() < 5,
        "weekday": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[day.weekday()],
        "holiday": "",
        "solar_term": "",
        "lunar": "",
        "festivals": [],
    }
    try:
        import chinese_calendar

        result["is_workday"] = chinese_calendar.is_workday(day)
        _, holiday = chinese_calendar.get_holiday_detail(day)
        result["holiday"] = holiday or ""
    except (ImportError, ValueError, NotImplementedError) as exc:
        if day not in _warned_days:
            logger.warning(
                "[生活日程] %s 的节假日数据不可用，按工作日和周末判断；可升级 chinesecalendar：%s",
                day,
                describe_error(exc),
            )
            _warned_days.add(day)
    try:
        from lunar_python import Solar

        lunar = Solar.fromYmd(day.year, day.month, day.day).getLunar()
        result["lunar"] = lunar.toString()
        result["festivals"] = list(dict.fromkeys(list(lunar.getFestivals()) + list(lunar.getOtherFestivals())))
        exact = lunar.getJieQi()
        if exact:
            result["solar_term"] = exact
        else:
            upcoming = lunar.getNextJieQi()
            solar = upcoming.getSolar()
            term_day = date(solar.getYear(), solar.getMonth(), solar.getDay())
            result["solar_term"] = f"{upcoming.getName()}前{(term_day - day).days}天"
    except Exception:
        # Lunar metadata is optional and does not affect schedule generation.
        pass
    return result
