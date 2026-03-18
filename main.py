from __future__ import annotations

import asyncio
import html
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register


WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

WEATHER_CODE_MAP = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "冰粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴夹小冰雹",
    99: "强雷暴夹冰雹",
}


@register(
    "astrbot_plugin_daliy",
    "OpenAI",
    "Telegram 每日晨报插件",
    "0.1.0",
    "https://github.com/zzzwannasleep/astrbot_plugin_daliy",
)
class DailyMorningReportPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._geo_cache: dict[str, dict[str, Any]] = {}
        self._state_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task | None = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 完成加载时启动定时任务。"""
        await self._load_subscriptions()
        await self._maybe_send_startup_catchup()
        self._start_scheduler()

    async def terminate(self):
        """插件卸载或停用时清理后台任务。"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._scheduler_task

    @filter.command_group("daily", alias={"morning", "晨报"})
    def daily(self):
        """每日晨报相关指令。"""
        pass

    @daily.command("subscribe", alias={"sub", "订阅"})
    async def subscribe(self, event: AstrMessageEvent, city: str = ""):
        """订阅当前会话的每日晨报。"""
        async for result in self._subscribe_impl(event, city):
            yield result

    @filter.command("dailysubscribe")
    async def daily_subscribe(self, event: AstrMessageEvent, city: str = ""):
        """订阅当前会话的每日晨报。"""
        async for result in self._subscribe_impl(event, city):
            yield result

    async def _subscribe_impl(self, event: AstrMessageEvent, city: str = ""):
        await self._maybe_delete_trigger_message(event)
        await self._upsert_subscription(event, city.strip())
        effective_city = self._resolve_city(city.strip())
        yield event.plain_result(
            "\n".join(
                [
                    "已订阅每日晨报。",
                    f"会话标识: {event.unified_msg_origin}",
                    f"天气城市: {effective_city or '未设置'}",
                    f"发送时间: {self._delivery_time_text()} ({self._timezone_name()})",
                ]
            )
        )

    @daily.command("unsubscribe", alias={"unsub", "退订", "取消订阅"})
    async def unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的晨报订阅。"""
        async for result in self._unsubscribe_impl(event):
            yield result

    @filter.command("dailyunsubscribe")
    async def daily_unsubscribe(self, event: AstrMessageEvent):
        """取消当前会话的晨报订阅。"""
        async for result in self._unsubscribe_impl(event):
            yield result

    async def _unsubscribe_impl(self, event: AstrMessageEvent):
        await self._maybe_delete_trigger_message(event)
        removed = await self._remove_subscription(event.unified_msg_origin)
        if removed:
            yield event.plain_result("已取消当前会话的每日晨报订阅。")
        else:
            yield event.plain_result("当前会话未订阅每日晨报。")

    @daily.command("city", alias={"城市"})
    async def set_city(self, event: AstrMessageEvent, city: str):
        """设置当前会话的天气城市。"""
        async for result in self._set_city_impl(event, city):
            yield result

    @filter.command("dailycity")
    async def daily_city(self, event: AstrMessageEvent, city: str):
        """设置当前会话的天气城市。"""
        async for result in self._set_city_impl(event, city):
            yield result

    async def _set_city_impl(self, event: AstrMessageEvent, city: str):
        await self._maybe_delete_trigger_message(event)
        updated = await self._set_subscription_city(event, city.strip())
        if not updated:
            yield event.plain_result("当前会话还没有订阅，请先执行 `/dailysubscribe`。")
            return
        yield event.plain_result(f"当前会话的天气城市已设置为: {city.strip()}")

    @daily.command("preview", alias={"test", "show", "预览", "测试"})
    async def preview(self, event: AstrMessageEvent, city: str = ""):
        """预览当前会话的晨报内容。"""
        async for result in self._preview_impl(event, city):
            yield result

    @filter.command("dailypreview")
    async def daily_preview(self, event: AstrMessageEvent, city: str = ""):
        """预览当前会话的晨报内容。"""
        async for result in self._preview_impl(event, city):
            yield result

    async def _preview_impl(self, event: AstrMessageEvent, city: str = ""):
        await self._maybe_delete_trigger_message(event)
        resolved_city = city.strip() or self._city_for_subscription(event.unified_msg_origin)
        payload = await self._build_report_payload(resolved_city)
        if payload["mode"] == "image":
            yield event.image_result(payload["content"])
            return
        yield event.plain_result(payload["content"])

    @daily.command("news", alias={"rss", "新闻"})
    async def news(self, event: AstrMessageEvent):
        """查看当前 RSS 新闻速览。"""
        async for result in self._news_impl(event):
            yield result

    @filter.command("dailynews")
    async def daily_news(self, event: AstrMessageEvent):
        """查看当前 RSS 新闻速览。"""
        async for result in self._news_impl(event):
            yield result

    async def _news_impl(self, event: AstrMessageEvent):
        await self._maybe_delete_trigger_message(event)
        payload = await self._build_news_payload()
        if payload["mode"] == "image":
            yield event.image_result(payload["content"])
            return
        yield event.plain_result(payload["content"])

    @daily.command("status", alias={"info", "状态"})
    async def status(self, event: AstrMessageEvent):
        """查看当前插件配置和订阅状态。"""
        async for result in self._status_impl(event):
            yield result

    @filter.command("dailystatus")
    async def daily_status(self, event: AstrMessageEvent):
        """查看当前插件配置和订阅状态。"""
        async for result in self._status_impl(event):
            yield result

    async def _status_impl(self, event: AstrMessageEvent):
        await self._maybe_delete_trigger_message(event)
        subscriptions = await self._get_subscription_snapshot()
        current = subscriptions.get(event.unified_msg_origin)
        lines = [
            f"启用状态: {'开启' if self._is_enabled() else '关闭'}",
            f"发送时间: {self._delivery_time_text()} ({self._timezone_name()})",
            f"图片模式: {'开启' if self._image_mode_enabled() else '关闭'}",
            f"TG 自动删命令: {'开启' if self._auto_delete_command_on_telegram() else '关闭'}",
            f"默认城市: {self._default_city() or '未设置'}",
            f"RSS 源数量: {len(self._rss_urls())}",
            f"总订阅数: {len(subscriptions)}",
            f"当前会话已订阅: {'是' if current else '否'}",
        ]
        if current:
            lines.append(f"当前会话城市: {current.get('city') or self._default_city() or '未设置'}")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @daily.command("sendnow", alias={"now", "broadcast", "立即发送", "群发"})
    async def sendnow(self, event: AstrMessageEvent):
        """管理员手动触发一次晨报群发。"""
        async for result in self._sendnow_impl(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("dailysendnow")
    async def daily_sendnow(self, event: AstrMessageEvent):
        """管理员手动触发一次晨报群发。"""
        async for result in self._sendnow_impl(event):
            yield result

    async def _sendnow_impl(self, event: AstrMessageEvent):
        await self._maybe_delete_trigger_message(event)
        success_count = await self._broadcast_daily_report(reason="manual")
        yield event.plain_result(f"晨报已尝试发送，成功投递到 {success_count} 个会话。")

    def _start_scheduler(self):
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def _scheduler_loop(self):
        while True:
            try:
                if not self._is_enabled():
                    await asyncio.sleep(60)
                    continue

                now = datetime.now(self._timezone())
                next_run = self._next_run_datetime(now)
                logger.info("晨报插件下一次发送时间: %s", next_run.isoformat())
                await self._sleep_until(next_run)

                if not self._is_enabled():
                    continue

                today_key = datetime.now(self._timezone()).date().isoformat()
                last_delivery = await self.get_kv_data("last_delivery_date", "")
                if last_delivery == today_key:
                    continue

                success_count = await self._broadcast_daily_report(reason="schedule")
                if success_count > 0:
                    await self.put_kv_data("last_delivery_date", today_key)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("晨报定时任务异常: %s", exc)
                await asyncio.sleep(60)

    async def _sleep_until(self, target: datetime):
        while True:
            now = datetime.now(target.tzinfo)
            remaining = (target - now).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 60))

    async def _maybe_send_startup_catchup(self):
        if not self.config.get("send_startup_catchup", False):
            return

        tz = self._timezone()
        now = datetime.now(tz)
        scheduled = now.replace(
            hour=self._delivery_hour(),
            minute=self._delivery_minute(),
            second=0,
            microsecond=0,
        )
        last_delivery = await self.get_kv_data("last_delivery_date", "")
        if now >= scheduled and last_delivery != now.date().isoformat():
            success_count = await self._broadcast_daily_report(reason="startup-catchup")
            if success_count > 0:
                await self.put_kv_data("last_delivery_date", now.date().isoformat())

    async def _broadcast_daily_report(self, reason: str) -> int:
        subscriptions = await self._get_subscription_snapshot()
        if not subscriptions:
            logger.info("晨报插件跳过发送，当前没有订阅会话。")
            return 0

        payload_cache: dict[str, dict[str, str]] = {}
        success_count = 0

        for unified_msg_origin, info in subscriptions.items():
            city = (info.get("city") or self._default_city()).strip()
            cache_key = city or "__default__"
            if cache_key not in payload_cache:
                try:
                    payload_cache[cache_key] = await self._build_report_payload(city)
                except Exception as exc:
                    logger.exception("晨报内容构建失败: city=%s error=%s", city, exc)
                    fallback_report = self._fallback_report()
                    payload_cache[cache_key] = {
                        "mode": "text",
                        "content": fallback_report,
                        "report": fallback_report,
                    }

            try:
                chain = self._build_message_chain(payload_cache[cache_key])
                await self.context.send_message(unified_msg_origin, chain)
                success_count += 1
            except Exception as exc:
                logger.warning(
                    "晨报发送失败: reason=%s target=%s error=%s",
                    reason,
                    unified_msg_origin,
                    exc,
                )

        logger.info("晨报发送完成: reason=%s success=%s", reason, success_count)
        return success_count

    async def _build_report_payload(self, city: str = "") -> dict[str, str]:
        report = await self._build_report(city)
        return await self._build_text_payload(report)

    async def _build_news_payload(self) -> dict[str, str]:
        news_text = await self._build_news_text()
        return await self._build_text_payload(news_text)

    async def _build_text_payload(self, text: str) -> dict[str, str]:
        if not self._image_mode_enabled():
            return {
                "mode": "text",
                "content": text,
            }

        try:
            image_path = await self.text_to_image(text, return_url=False)
            return {
                "mode": "image",
                "content": image_path,
            }
        except Exception as exc:
            logger.exception("文本渲染图片失败，已回退为文本模式: %s", exc)
            return {
                "mode": "text",
                "content": text,
            }

    def _build_message_chain(self, payload: dict[str, str]) -> MessageChain:
        chain = MessageChain()
        if payload.get("mode") == "image":
            return chain.file_image(payload["content"])
        return chain.message(payload["content"])

    async def _maybe_delete_trigger_message(self, event: AstrMessageEvent):
        if not self._auto_delete_command_on_telegram():
            return
        if event.get_platform_name() != "telegram":
            return

        raw_update = getattr(event.message_obj, "raw_message", None)
        telegram_message = getattr(raw_update, "message", None)
        if not telegram_message:
            return

        chat = getattr(telegram_message, "chat", None)
        chat_id = getattr(chat, "id", None)
        message_id = getattr(telegram_message, "message_id", None)
        if chat_id is None or message_id is None:
            return

        try:
            delete_method = getattr(telegram_message, "delete", None)
            if callable(delete_method):
                await delete_method()
                return

            client = getattr(event, "client", None)
            if client:
                await client.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            logger.warning("Telegram 删除命令消息失败: chat_id=%s message_id=%s error=%s", chat_id, message_id, exc)

    async def _build_report(self, city: str = "") -> str:
        resolved_city = city.strip() or self._default_city()
        task_map: dict[str, asyncio.Task] = {}

        async with self._http_client() as client:
            if self.config.get("include_weather", True) and resolved_city:
                task_map["weather"] = asyncio.create_task(
                    self._fetch_weather_summary(client, resolved_city)
                )
            if self.config.get("include_quote", True):
                task_map["quote"] = asyncio.create_task(self._fetch_hitokoto(client))
            if self.config.get("include_poem", False):
                task_map["poem"] = asyncio.create_task(self._fetch_poem(client))
            if self._rss_urls() and self._news_limit() > 0:
                task_map["news"] = asyncio.create_task(self._fetch_headlines(client))

            results = await asyncio.gather(*task_map.values(), return_exceptions=True)

        sections = dict(zip(task_map.keys(), results))
        now = datetime.now(self._timezone())
        lines = [
            f"{self.config.get('report_title', '每日晨报')}",
            f"{now:%Y-%m-%d} 星期{WEEKDAY_CN[now.weekday()]}",
        ]

        weather = self._result_or_none("weather", sections)
        news = self._result_or_none("news", sections)
        quote = self._result_or_none("quote", sections)
        poem = self._result_or_none("poem", sections)

        if weather:
            lines.extend(["", "天气", weather])

        if news:
            lines.extend(["", "新闻速览"])
            for index, item in enumerate(news, start=1):
                title = item.get("title", "").strip()
                source = item.get("source", "").strip()
                if title:
                    suffix = f" [{source}]" if source else ""
                    lines.append(f"{index}. {title}{suffix}")

        if quote:
            lines.extend(["", "今日一句", quote])

        if poem:
            lines.extend(["", "诗词", poem])

        footer = str(self.config.get("footer", "") or "").strip()
        if footer:
            lines.extend(["", footer])

        if len(lines) <= 2:
            lines.extend(["", "今天的外部数据暂时拉取失败，请检查网络、RSS 源或接口配置。"])

        return "\n".join(lines)

    async def _build_news_text(self) -> str:
        now = datetime.now(self._timezone())
        lines = [
            "新闻速览",
            f"{now:%Y-%m-%d} 星期{WEEKDAY_CN[now.weekday()]}",
        ]

        try:
            async with self._http_client() as client:
                news = await self._fetch_headlines(client)
        except Exception as exc:
            logger.exception("新闻速览拉取失败: %s", exc)
            news = []

        if news:
            lines.append("")
            for index, item in enumerate(news, start=1):
                title = item.get("title", "").strip()
                source = item.get("source", "").strip()
                if title:
                    suffix = f" [{source}]" if source else ""
                    lines.append(f"{index}. {title}{suffix}")
        else:
            lines.extend(["", "当前没有可用新闻，请检查 RSS 源或接口配置。"])

        footer = str(self.config.get("footer", "") or "").strip()
        if footer:
            lines.extend(["", footer])

        return "\n".join(lines)

    def _fallback_report(self) -> str:
        now = datetime.now(self._timezone())
        lines = [
            f"{self.config.get('report_title', '每日晨报')}",
            f"{now:%Y-%m-%d} 星期{WEEKDAY_CN[now.weekday()]}",
            "",
            "晨报暂时生成失败，请检查网络、RSS 源或接口配置。",
        ]
        footer = str(self.config.get("footer", "") or "").strip()
        if footer:
            lines.extend(["", footer])
        return "\n".join(lines)

    def _result_or_none(self, key: str, results: dict[str, Any]) -> Any:
        value = results.get(key)
        if isinstance(value, Exception):
            logger.warning("晨报数据块拉取失败: %s error=%s", key, value)
            return None
        return value

    async def _fetch_weather_summary(
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

        weather_code = self._first_or_default(daily.get("weather_code"), current.get("weather_code"))
        max_temp = self._first_or_default(daily.get("temperature_2m_max"))
        min_temp = self._first_or_default(daily.get("temperature_2m_min"))
        rain_prob = self._first_or_default(daily.get("precipitation_probability_max"))
        sunrise = self._format_time(self._first_or_default(daily.get("sunrise"), ""))
        sunset = self._format_time(self._first_or_default(daily.get("sunset"), ""))
        current_temp = current.get("temperature_2m")

        location_name = geo.get("display_name") or city_name
        weather_text = WEATHER_CODE_MAP.get(int(weather_code), "未知天气") if weather_code is not None else "未知天气"
        parts = [f"{location_name}: {weather_text}"]

        if min_temp is not None and max_temp is not None:
            parts.append(f"{round(min_temp)}~{round(max_temp)}°C")
        if current_temp is not None:
            parts.append(f"当前 {round(current_temp)}°C")
        if rain_prob is not None:
            parts.append(f"降水概率 {rain_prob}%")
        if sunrise:
            parts.append(f"日出 {sunrise}")
        if sunset:
            parts.append(f"日落 {sunset}")

        return "，".join(parts)

    async def _fetch_city_geo(
        self, client: httpx.AsyncClient, city_name: str
    ) -> dict[str, Any] | None:
        cache_key = city_name.lower()
        if cache_key in self._geo_cache:
            return self._geo_cache[cache_key]

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
        self._geo_cache[cache_key] = result
        return result

    async def _fetch_headlines(self, client: httpx.AsyncClient) -> list[dict[str, str]]:
        news_limit = self._news_limit()
        items: list[dict[str, str]] = []
        seen_titles: set[str] = set()

        for url in self._rss_urls():
            try:
                response = await client.get(url)
                response.raise_for_status()
                feed = feedparser.parse(response.text)
                source = self._clean_text(feed.feed.get("title", "") or "")
            except Exception as exc:
                logger.warning("RSS 拉取失败: url=%s error=%s", url, exc)
                continue

            for entry in feed.entries:
                title = self._clip_text(self._clean_text(entry.get("title", "") or ""), 80)
                if not title:
                    continue
                key = title.casefold()
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                items.append(
                    {
                        "title": title,
                        "source": source,
                    }
                )
                if len(items) >= news_limit:
                    return items

        return items

    async def _fetch_hitokoto(self, client: httpx.AsyncClient) -> str | None:
        response = await client.get(
            "https://v1.hitokoto.cn/",
            params={"encode": "json", "max_length": 60},
        )
        response.raise_for_status()
        data = response.json()

        text = self._clean_text(data.get("hitokoto", "") or "")
        from_name = self._clean_text(
            data.get("from_who") or data.get("from") or data.get("creator", "") or ""
        )
        if not text:
            return None
        return f"{text} —— {from_name}" if from_name else text

    async def _fetch_poem(self, client: httpx.AsyncClient) -> str | None:
        response = await client.get("https://v2.jinrishici.com/one.json")
        response.raise_for_status()
        data = response.json().get("data", {})
        content = self._clean_text(data.get("content", "") or "")
        origin = data.get("origin", {}) or {}
        title = self._clean_text(origin.get("title", "") or "")
        author = self._clean_text(origin.get("author", "") or "")

        if not content:
            return None

        meta = "".join(
            part
            for part in [
                f"《{title}》" if title else "",
                author if author else "",
            ]
        )
        return f"{content} —— {meta}" if meta else content

    def _http_client(self) -> httpx.AsyncClient:
        proxy = str(self.config.get("http_proxy", "") or "").strip() or None
        timeout = max(int(self.config.get("http_timeout_seconds", 15) or 15), 5)
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "follow_redirects": True,
            "trust_env": True,
            "headers": {
                "User-Agent": "astrbot_plugin_daliy/0.1.0",
            },
        }
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def _load_subscriptions(self):
        data = await self.get_kv_data("subscriptions", {})
        if not isinstance(data, dict):
            logger.warning("晨报插件订阅数据格式异常，已重置为空。")
            data = {}
        async with self._state_lock:
            self._subscriptions = data

    async def _get_subscription_snapshot(self) -> dict[str, dict[str, Any]]:
        async with self._state_lock:
            return {key: value.copy() for key, value in self._subscriptions.items()}

    async def _persist_subscriptions(self):
        async with self._state_lock:
            data = {key: value.copy() for key, value in self._subscriptions.items()}
        await self.put_kv_data("subscriptions", data)

    async def _upsert_subscription(self, event: AstrMessageEvent, city: str):
        async with self._state_lock:
            self._subscriptions[event.unified_msg_origin] = {
                "city": city,
                "sender_name": event.get_sender_name(),
                "updated_at": datetime.now(self._timezone()).isoformat(timespec="seconds"),
            }
        await self._persist_subscriptions()

    async def _remove_subscription(self, unified_msg_origin: str) -> bool:
        removed = False
        async with self._state_lock:
            removed = unified_msg_origin in self._subscriptions
            if removed:
                self._subscriptions.pop(unified_msg_origin, None)
        if removed:
            await self._persist_subscriptions()
        return removed

    async def _set_subscription_city(self, event: AstrMessageEvent, city: str) -> bool:
        async with self._state_lock:
            item = self._subscriptions.get(event.unified_msg_origin)
            if not item:
                return False
            item["city"] = city
            item["sender_name"] = event.get_sender_name()
            item["updated_at"] = datetime.now(self._timezone()).isoformat(timespec="seconds")
        await self._persist_subscriptions()
        return True

    def _city_for_subscription(self, unified_msg_origin: str) -> str:
        item = self._subscriptions.get(unified_msg_origin, {})
        return (item.get("city") or self._default_city()).strip()

    def _resolve_city(self, city: str) -> str:
        return city.strip() or self._default_city()

    def _default_city(self) -> str:
        return str(self.config.get("default_city", "") or "").strip()

    def _delivery_time_text(self) -> str:
        return f"{self._delivery_hour():02d}:{self._delivery_minute():02d}"

    def _delivery_hour(self) -> int:
        return self._parse_delivery_time()[0]

    def _delivery_minute(self) -> int:
        return self._parse_delivery_time()[1]

    def _parse_delivery_time(self) -> tuple[int, int]:
        raw = str(self.config.get("delivery_time", "08:00") or "08:00").strip()
        try:
            hour_text, minute_text = raw.split(":", 1)
            hour = min(max(int(hour_text), 0), 23)
            minute = min(max(int(minute_text), 0), 59)
            return hour, minute
        except Exception:
            logger.warning("无效的 delivery_time 配置: %s，已回退到 08:00", raw)
            return 8, 0

    def _timezone_name(self) -> str:
        value = str(self.config.get("delivery_timezone", "Asia/Shanghai") or "").strip()
        return value or "Asia/Shanghai"

    def _timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._timezone_name())
        except Exception:
            logger.warning(
                "无效的时区配置: %s，已回退到 Asia/Shanghai",
                self._timezone_name(),
            )
            return ZoneInfo("Asia/Shanghai")

    def _next_run_datetime(self, now: datetime) -> datetime:
        candidate = now.replace(
            hour=self._delivery_hour(),
            minute=self._delivery_minute(),
            second=0,
            microsecond=0,
        )
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    def _rss_urls(self) -> list[str]:
        raw = str(self.config.get("rss_urls", "") or "")
        return [
            line.strip()
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

    def _news_limit(self) -> int:
        value = int(self.config.get("news_limit", 5) or 5)
        return max(value, 0)

    def _is_enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _image_mode_enabled(self) -> bool:
        return bool(self.config.get("image_mode_enabled", False))

    def _auto_delete_command_on_telegram(self) -> bool:
        return bool(self.config.get("auto_delete_command_on_telegram", False))

    @staticmethod
    def _clean_text(text: str) -> str:
        return " ".join(html.unescape(text).split())

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return f"{text[: limit - 1]}…"

    @staticmethod
    def _first_or_default(value: Any, default: Any = None) -> Any:
        if isinstance(value, list):
            return value[0] if value else default
        return value if value is not None else default

    @staticmethod
    def _format_time(value: str) -> str:
        if "T" not in value:
            return ""
        return value.split("T", 1)[1][:5]
