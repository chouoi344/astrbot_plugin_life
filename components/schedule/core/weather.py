"""Open-Meteo weather with city coordinates and persistent offline fallback."""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from datetime import datetime

import aiohttp

from .errors import describe_error

logger = logging.getLogger("astrbot")
SOUTH_REGIONS = (
    "广东", "广州", "深圳", "福建", "泉州", "厦门", "浙江", "杭州", "江苏", "上海", "湖南", "湖北",
    "江西", "四川", "重庆", "云南", "贵州", "广西", "海南", "港澳台",
)
NORTH_REGIONS = (
    "北京", "天津", "河北", "山西", "内蒙", "辽宁", "吉林", "黑龙江", "山东", "河南", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "东北", "华北", "西北",
)
WMO_ZH = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "小冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴小冰雹",
    99: "雷雨伴大冰雹",
}


def wmo_to_zh(code) -> str:
    try:
        return WMO_ZH.get(int(code), "天气不明")
    except (TypeError, ValueError):
        return "天气不明"


def significant_change(previous: dict, current: dict) -> bool:
    if (
        previous.get("weather_source", previous.get("source")) == "ai"
        or current.get("weather_source", current.get("source")) == "ai"
    ):
        return False
    old_rain = previous.get("current", {}).get("precip_probability", previous.get("precip_probability"))
    new_rain = current.get("current", {}).get("precip_probability", current.get("precip_probability"))
    old_temp = previous.get("current", {}).get("temperature", previous.get("temp_max"))
    new_temp = current.get("current", {}).get("temperature", current.get("temp_max"))
    rain_changed = (
        isinstance(new_rain, (int, float)) and new_rain >= 70 and isinstance(old_rain, (int, float)) and old_rain < 70
    )
    temp_changed = (
        isinstance(old_temp, (int, float)) and isinstance(new_temp, (int, float)) and old_temp - new_temp >= 6
    )
    return rain_changed or temp_changed


class WeatherService:
    def __init__(self, config, store, tz, persona_provider=None):
        self.config, self.store, self.tz = config, store, tz
        self.persona_provider = persona_provider
        cache = store.read("weather_cache.json", {})
        self._cache = cache if isinstance(cache, dict) else {}
        self._lock = asyncio.Lock()
        self._reported_fallbacks = set()
        self._location_revision = 0

    @property
    def settings(self):
        value = self.config.get("weather", {})
        return value if isinstance(value, dict) else {}

    def cached(self, now=None) -> dict:
        weather = self._cache.get("weather")
        matches = self.matches_location(weather)
        if matches and weather.get("location_signature"):
            matches = weather["location_signature"] == self._location_signature()
        # Update the cached hourly slice locally; ordinary chat performs no I/O.
        if matches and weather:
            result = copy.deepcopy(weather)
            if now is not None:
                hours = result.pop("hours", [])
                local = now.astimezone(self.tz)
                for row in hours:
                    if row.get("time", "")[:13] == local.isoformat()[:13]:
                        result["current"] = copy.deepcopy(row)
                        break
            else:
                result.pop("hours", None)
            return result
        return {
            "summary": "待按季节和城市推测（AI 编的天气）",
            "temp_min": None,
            "temp_max": None,
            "precip_probability": None,
            "source": "ai",
            "weather_source": "ai",
            "periods": {},
            "current": {},
            "city": str(self.settings.get("city", "") or ""),
        }

    def matches_location(self, weather):
        city = str(self.settings.get("city", "") or "").strip()
        return isinstance(weather, dict) and str(weather.get("fallback_from") or weather.get("city", city)).strip() == city

    def using_fallback(self):
        return any(
            self.matches_location(value) and bool(value.get("fallback_from"))
            for value in (self._cache.get("coordinates"), self._cache.get("weather"))
        )

    def _location_signature(self):
        city = str(self.settings.get("city", "") or "").strip()
        return city + "|geocoding"

    def invalidate_location(self):
        """Invalidate both memory and disk before a newly selected city loads."""
        self._location_revision += 1
        self._cache.pop("coordinates", None)
        self._cache.pop("weather", None)
        try:
            self.store.write("weather_cache.json", self._cache)
        except OSError as exc:
            logger.warning("天气位置缓存保存失败，将按新城市重新查询：%s", describe_error(exc))

    def remember_ai(self, weather: dict) -> None:
        """Retain a generated same-location forecast through later network failures."""
        if not isinstance(weather, dict) or not str(weather.get("summary", "") or "").strip():
            return
        if str(weather.get("city", self.settings.get("city", "")) or "").strip() != str(self.settings.get("city", "") or "").strip():
            return
        weather = copy.deepcopy(weather)
        weather.update(source="ai", weather_source="ai", location_signature=self._location_signature())
        weather["city"] = str(self.settings.get("city", "") or "")
        weather.pop("hours", None)
        self._cache["weather"] = weather
        self.store.write("weather_cache.json", self._cache)

    async def _request(self, url, params):
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                result = await response.json()
                if not isinstance(result, dict):
                    raise ValueError("天气接口返回了非对象数据")
                return result

    async def _coordinates(self):
        city = str(self.settings.get("city", "") or "").strip()
        if not city:
            raise ValueError("天气城市尚未设置")
        cached = self._cache.get("coordinates", {})
        if cached.get("fallback_from") and cached.get("city") != await self._reference_city():
            self._cache.pop("coordinates", None)
            self._cache.pop("weather", None)
            cached = {}
        revision = self._location_revision
        if self.matches_location(cached) and cached.get("city") and self._coordinate_pair(cached) is not None:
            return cached
        errors = []
        attempted = []
        for fallback in (False, True):
            # Persona access is a local settings read; it never calls a model.
            target = await self._reference_city() if fallback else city
            for candidate in self._city_candidates(target):
                if candidate in attempted:
                    continue
                attempted.append(candidate)
                try:
                    result = await self._request(
                        "https://geocoding-api.open-meteo.com/v1/search",
                        {"name": candidate, "count": 5, "language": "zh", "format": "json"},
                    )
                except Exception as exc:
                    errors.append((candidate, exc))
                    continue
                matches = result.get("results") if isinstance(result, dict) else None
                if not isinstance(matches, list):
                    continue
                for match in matches:
                    pair = self._coordinate_pair(match)
                    if pair is None:
                        continue
                    coordinates = {"city": target, "latitude": pair[0], "longitude": pair[1]}
                    if fallback:
                        coordinates["fallback_from"] = city
                    if revision == self._location_revision and city == str(self.settings.get("city", "") or "").strip():
                        self._cache["coordinates"] = coordinates
                        try:
                            self.store.write("weather_cache.json", self._cache)
                        except OSError as exc:
                            logger.warning("天气坐标缓存保存失败，本次继续查询真实天气：%s", describe_error(exc))
                        if fallback and (city, target) not in self._reported_fallbacks:
                            logger.info("查不到天气城市 %s，已换为 %s 的真实天气", city, target)
                            self._reported_fallbacks.add((city, target))
                    return coordinates
            if revision != self._location_revision or city != str(self.settings.get("city", "") or "").strip():
                raise ValueError("天气城市已更新，等待新城市查询")
        attempted = "、".join(attempted)
        if errors:
            reasons = "；".join(
                name + "：" + describe_error(error, operation="城市查询", timeout_seconds=10)
                for name, error in errors
            )
            raise ValueError("查询天气城市失败：" + city + "（已尝试 " + attempted + "）；" + reasons) from errors[-1][
                1
            ]
        raise ValueError("查不到天气城市：" + city + "（已尝试 " + attempted + "）")

    async def _reference_city(self):
        profile = self.config.get("profile") or {}
        living = str(profile.get("living", "") or "") if isinstance(profile, dict) else ""
        persona = ""
        if self.persona_provider:
            try:
                persona = str(await self.persona_provider() or "")
            except Exception as exc:
                logger.warning("读取天气参考地域失败，先参考生活档案：%s", describe_error(exc))
        # Latest explicit geography takes priority over historical living data.
        for description in (persona, living):
            south = any(word in description for word in SOUTH_REGIONS)
            north = any(word in description for word in NORTH_REGIONS)
            if south or north:
                return "广州" if south and not north else "北京"
        return "北京"

    @staticmethod
    def _city_candidates(city):
        candidates = [city]
        if "市" not in city:
            candidates.append(city + "市")
        if city.endswith(("市", "县", "区")):
            candidates.append(city[:-1])
        return list(dict.fromkeys(candidate for candidate in candidates if candidate))

    @staticmethod
    def _coordinate_pair(value):
        if not isinstance(value, dict):
            return None
        raw_lat, raw_lon = value.get("latitude"), value.get("longitude")
        if isinstance(raw_lat, bool) or isinstance(raw_lon, bool):
            return None
        try:
            latitude, longitude = float(raw_lat), float(raw_lon)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            return None
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return None
        return latitude, longitude

    async def refresh(self, now: datetime) -> dict:
        async with self._lock:
            if not self.settings.get("enabled", True):
                return self.cached(now)
            signature, revision = self._location_signature(), self._location_revision
            try:
                coordinates = await self._coordinates()
                if signature != self._location_signature() or revision != self._location_revision:
                    return self.cached(now)
                params = {
                    "latitude": coordinates["latitude"],
                    "longitude": coordinates["longitude"],
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "hourly": "temperature_2m,weather_code,precipitation_probability",
                    "timezone": str(self.tz),
                    "forecast_days": 2,
                }
                payload = await self._request("https://api.open-meteo.com/v1/forecast", params)
                if signature != self._location_signature() or revision != self._location_revision:
                    return self.cached(now)
                weather = self._parse(payload, now, coordinates.get("city", ""))
                if coordinates.get("fallback_from"):
                    weather["fallback_from"] = coordinates["fallback_from"]
                weather["location_signature"] = signature
                self._cache["weather"] = weather
                try:
                    self.store.write("weather_cache.json", self._cache)
                except OSError as exc:
                    logger.warning("天气缓存保存失败，本次使用已取得的真实天气：%s", describe_error(exc))
                return self.cached(now)
            except Exception as exc:
                logger.warning(
                    "天气更新失败，保留缓存并等待下次更新：%s",
                    describe_error(exc, operation="天气请求", timeout_seconds=10),
                )
                fallback = self.cached(now)
                if signature != self._location_signature() or revision != self._location_revision:
                    return fallback
                if (self.settings.get("city") and fallback.get("source") == "ai"
                        and "待按季节" in str(fallback.get("summary", ""))):
                    # A failed real-weather request must never ask the model to invent a forecast.
                    fallback.update(summary="天气暂时获取失败，稍后自动重试", source="unavailable", weather_source="unavailable",
                                    location_signature=signature)
                    coordinates = self._cache.get("coordinates", {})
                    if self.matches_location(coordinates) and coordinates.get("fallback_from"):
                        fallback.update(city=coordinates["city"], fallback_from=coordinates["fallback_from"])
                    self._cache["weather"] = copy.deepcopy(fallback)
                    try:
                        self.store.write("weather_cache.json", self._cache)
                    except OSError as cache_exc:
                        logger.warning("天气状态缓存保存失败：%s", describe_error(cache_exc))
                elif fallback.get("source") not in ("ai", "unavailable"):
                    fallback["source"] = "cache"
                return fallback

    def _parse(self, payload, now, city):
        local = now.astimezone(self.tz)
        daily = payload.get("daily") or {}
        times = daily.get("time") or []
        if not times:
            raise ValueError("天气接口缺少每日数据")
        day_text = local.date().isoformat()
        index = times.index(day_text) if day_text in times else 0

        def at(key):
            values = daily.get(key) or []
            return values[index] if index < len(values) else None

        hourly = payload.get("hourly") or {}
        hours = []
        for pos, time in enumerate(hourly.get("time") or []):

            def hv(key):
                values = hourly.get(key) or []
                return values[pos] if pos < len(values) else None

            hours.append(
                {
                    "time": time,
                    "temperature": hv("temperature_2m"),
                    "summary": wmo_to_zh(hv("weather_code")),
                    "precip_probability": hv("precipitation_probability"),
                }
            )
        periods = {}
        for name, bounds in (("morning", (6, 12)), ("afternoon", (12, 18)), ("evening", (18, 24))):
            rows = [
                row
                for row in hours
                if row["time"][:10] == times[index] and bounds[0] <= int(row["time"][11:13]) < bounds[1]
            ]
            temperatures = [row["temperature"] for row in rows if isinstance(row["temperature"], (float, int))]
            probabilities = [
                row["precip_probability"] for row in rows if isinstance(row["precip_probability"], (float, int))
            ]
            summaries = list(dict.fromkeys(row["summary"] for row in rows))
            periods[name] = {
                "summary": "转".join(summaries[:3]),
                "temp_min": min(temperatures) if temperatures else None,
                "temp_max": max(temperatures) if temperatures else None,
                "precip_probability": max(probabilities) if probabilities else None,
            }
        current = next((row for row in hours if row["time"][:13] == local.isoformat()[:13]), {})
        return {
            "summary": wmo_to_zh(at("weather_code")),
            "temp_min": at("temperature_2m_min"),
            "temp_max": at("temperature_2m_max"),
            "precip_probability": at("precipitation_probability_max"),
            "source": "open-meteo",
            "weather_source": "open-meteo",
            "city": city,
            "periods": periods,
            "current": current,
            "hours": hours,
            "updated_at": local.isoformat(),
            "date": times[index],
        }
