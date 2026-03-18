from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

import httpx
from astrbot.api import logger

from daily_shared import WEATHER_CODE_MAP


class WeatherMixin:
    async def _fetch_weather_summary(
        self, client: httpx.AsyncClient, city_name: str
    ) -> str | None:
        provider = self._weather_provider()
        if provider == "uapi":
            try:
                return await self._fetch_uapi_weather_summary(client, city_name)
            except Exception as exc:
                logger.warning(
                    "UAPI 天气调用失败，已回退到 Open-Meteo: city=%s error=%s",
                    city_name,
                    exc,
                )
                return await self._fetch_open_meteo_weather_summary(client, city_name)
        if self._weather_provider() == "custom":
            try:
                return await self._fetch_custom_weather_summary(client, city_name)
            except Exception as exc:
                logger.warning(
                    "自定义天气 API 调用失败，已回退到 Open-Meteo: city=%s error=%s",
                    city_name,
                    exc,
                )
        return await self._fetch_open_meteo_weather_summary(client, city_name)

    async def _fetch_uapi_weather_summary(
        self, client: httpx.AsyncClient, city_name: str
    ) -> str | None:
        response = await client.get(
            "https://uapis.cn/api/v1/misc/weather",
            params={
                "city": city_name,
                "forecast": "true",
                "extended": "true",
            },
        )
        response.raise_for_status()
        data = response.json()

        location_name = (
            self._clean_text(str(data.get("city", "") or ""))
            or self._clean_text(str(data.get("province", "") or ""))
            or city_name
        )
        weather_text = self._clean_text(str(data.get("weather", "") or "")) or "未知天气"
        parts = [f"{location_name}: {weather_text}"]

        temp_min_value = self._safe_float(data.get("temp_min"))
        temp_max_value = self._safe_float(data.get("temp_max"))
        current_temp_value = self._safe_float(data.get("temperature"))
        humidity_text = self._text_value(data.get("humidity"))
        wind_direction = self._clean_text(str(data.get("wind_direction", "") or ""))
        wind_power = self._clean_text(str(data.get("wind_power", "") or ""))
        feels_like_value = self._safe_float(data.get("feels_like"))
        aqi_text_value = self._text_value(data.get("aqi"))
        aqi_category = self._clean_text(str(data.get("aqi_category", "") or ""))

        if temp_min_value is not None and temp_max_value is not None:
            parts.append(f"{round(temp_min_value)}~{round(temp_max_value)}°C")
        if current_temp_value is not None:
            parts.append(f"当前 {round(current_temp_value)}°C")
        if humidity_text:
            parts.append(f"湿度 {humidity_text}%")
        if wind_direction or wind_power:
            wind_text = " ".join(part for part in [wind_direction, wind_power] if part)
            if wind_text:
                parts.append(wind_text)
        if feels_like_value is not None:
            parts.append(f"体感 {round(feels_like_value)}°C")
        if aqi_text_value:
            aqi_text = f"AQI {aqi_text_value}"
            if aqi_category:
                aqi_text = f"{aqi_text} {aqi_category}"
            parts.append(aqi_text)

        return "，".join(parts)

    async def _fetch_open_meteo_weather_summary(
        self, client: httpx.AsyncClient, city_name: str
    ) -> str | None:
        geo = await self._fetch_city_geo(client, city_name)
        if not geo:
            return f"{city_name}: 未找到该城市的天气数据。"

        response = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": geo["latitude"],
                "longitude": geo["longitude"],
                "current": "temperature_2m,weather_code",
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max,sunrise,sunset"
                ),
                "forecast_days": 1,
                "timezone": self._timezone_name(),
            },
        )
        response.raise_for_status()
        data = response.json()

        current = data.get("current", {})
        daily = data.get("daily", {})

        weather_code = self._safe_int(
            self._first_or_default(daily.get("weather_code"), current.get("weather_code"))
        )
        max_temp = self._safe_float(self._first_or_default(daily.get("temperature_2m_max")))
        min_temp = self._safe_float(self._first_or_default(daily.get("temperature_2m_min")))
        rain_prob_value = self._safe_float(
            self._first_or_default(daily.get("precipitation_probability_max"))
        )
        sunrise = self._format_time(self._first_or_default(daily.get("sunrise"), ""))
        sunset = self._format_time(self._first_or_default(daily.get("sunset"), ""))
        current_temp = self._safe_float(current.get("temperature_2m"))

        location_name = geo.get("display_name") or city_name
        weather_text = (
            WEATHER_CODE_MAP.get(weather_code, "未知天气")
            if weather_code is not None
            else "未知天气"
        )
        parts = [f"{location_name}: {weather_text}"]

        if min_temp is not None and max_temp is not None:
            parts.append(f"{round(min_temp)}~{round(max_temp)}°C")
        if current_temp is not None:
            parts.append(f"当前 {round(current_temp)}°C")
        if rain_prob_value is not None:
            parts.append(f"降水概率 {round(rain_prob_value)}%")
        if sunrise:
            parts.append(f"日出 {sunrise}")
        if sunset:
            parts.append(f"日落 {sunset}")

        return "，".join(parts)

    async def _fetch_custom_weather_summary(
        self, client: httpx.AsyncClient, city_name: str
    ) -> str | None:
        template = self._custom_weather_api_url()
        if not template:
            raise ValueError("未配置 custom_weather_api_url")

        geo: dict[str, Any] | None = None
        if any(token in template for token in ("{latitude}", "{longitude}", "{display_name}")):
            geo = await self._fetch_city_geo(client, city_name)

        values = {
            "city": city_name,
            "city_urlencoded": quote_plus(city_name),
            "timezone": self._timezone_name(),
            "latitude": "" if not geo else str(geo.get("latitude", "")),
            "longitude": "" if not geo else str(geo.get("longitude", "")),
            "display_name": city_name if not geo else str(geo.get("display_name") or city_name),
        }
        request_url = self._fill_url_template(template, values)
        response = await client.get(
            request_url,
            headers=self._custom_weather_headers(),
        )
        response.raise_for_status()

        response_path = self._custom_weather_response_path()
        if response_path:
            data = response.json()
            value = self._extract_data_by_path(data, response_path)
            text = self._text_value(value)
            if text:
                return self._clip_text(text, 300)
            raise ValueError(f"自定义天气 API 返回中未找到可用字段: {response_path}")

        content_type = str(response.headers.get("content-type", "") or "").lower()
        if "json" in content_type:
            guessed = self._guess_weather_text_from_json(response.json())
            if guessed:
                return self._clip_text(guessed, 300)
            raise ValueError("自定义天气 API 返回 JSON，但未配置 custom_weather_response_path")

        text = self._clean_text(response.text)
        return self._clip_text(text, 300) if text else None

    async def _fetch_city_geo(
        self, client: httpx.AsyncClient, city_name: str
    ) -> dict[str, Any] | None:
        cache_key = city_name.strip().casefold()
        cached = self._geo_cache.pop(cache_key, None)
        if cached is not None:
            self._geo_cache[cache_key] = cached
            return cached.copy()

        response = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={
                "name": city_name,
                "count": 1,
                "language": "zh",
                "format": "json",
            },
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results") or []
        if not results:
            return None

        item = results[0]
        display_name = item.get("name", city_name)
        admin1 = item.get("admin1") or ""
        country = item.get("country") or ""
        if admin1 and admin1 != display_name:
            display_name = f"{display_name}, {admin1}"
        if country:
            display_name = f"{display_name}, {country}"

        result = {
            "latitude": item["latitude"],
            "longitude": item["longitude"],
            "display_name": display_name,
        }
        self._remember_geo_cache(cache_key, result)
        return result.copy()
