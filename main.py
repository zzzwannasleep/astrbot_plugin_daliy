from __future__ import annotations

import asyncio
import html
import json
import re
import sys
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from daily_shared import GEO_CACHE_MAX_SIZE, WEEKDAY_CN
from news_mixin import NewsMixin
from rendering_mixin import RenderingMixin
from scheduler_mixin import SchedulerMixin
from telegraph_mixin import TelegraphMixin
from weather_mixin import WeatherMixin


@register(
    "astrbot_plugin_daliy",
    "zzzwannasleep",
    "Telegram 每日晨报插件",
    "0.1.0",
    "https://github.com/zzzwannasleep/astrbot_plugin_daliy",
)
class DailyMorningReportPlugin(
    SchedulerMixin,
    WeatherMixin,
    NewsMixin,
    TelegraphMixin,
    RenderingMixin,
    Star,
):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._geo_cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._news_cache: dict[str, Any] | None = None
        self._state_lock = asyncio.Lock()
        self._news_cache_lock = asyncio.Lock()
        self._scheduler_task: asyncio.Task | None = None

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 完成加载时启动定时任务。"""
        await self._load_subscriptions()
        await self._maybe_send_startup_catchup()
        self._start_scheduler()

    async def terminate(self):
        """插件卸载或停用时清理后台任务。"""
        await self._stop_scheduler()

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
        yield event.plain_result(payload["content"])

    @daily.command("news", alias={"rss", "新闻"})
    async def news(self, event: AstrMessageEvent):
        """查看当前 RSS 新闻速览。"""
        async for result in self._news_impl(event):
            yield result

    @daily.command("weather", alias={"天气"})
    async def weather(self, event: AstrMessageEvent, city: str = ""):
        """查询指定城市的天气。"""
        async for result in self._weather_impl(event, city):
            yield result

    @filter.command("weather")
    async def quick_weather(self, event: AstrMessageEvent, city: str = ""):
        """查询指定城市的天气。"""
        async for result in self._weather_impl(event, city):
            yield result

    @filter.command("dailyweather")
    async def daily_weather(self, event: AstrMessageEvent, city: str = ""):
        """查询指定城市的天气。"""
        async for result in self._weather_impl(event, city):
            yield result

    @filter.command("dailynews")
    async def daily_news(self, event: AstrMessageEvent):
        """查看当前 RSS 新闻速览。"""
        async for result in self._news_impl(event):
            yield result

    async def _news_impl(self, event: AstrMessageEvent):
        await self._maybe_delete_trigger_message(event)
        payload = await self._build_news_payload()
        yield event.plain_result(payload["content"])

    async def _weather_impl(self, event: AstrMessageEvent, city: str = ""):
        await self._maybe_delete_trigger_message(event)
        resolved_city = city.strip() or self._city_for_subscription(event.unified_msg_origin)
        if not resolved_city:
            yield event.plain_result("请提供城市名，或先设置默认城市 / 当前会话城市。")
            return

        try:
            async with self._http_client() as client:
                weather = await self._fetch_weather_summary(client, resolved_city)
        except Exception as exc:
            logger.warning("天气查询失败: city=%s error=%s", resolved_city, exc)
            weather = None

        if weather:
            yield event.plain_result(weather)
        else:
            yield event.plain_result(f"{resolved_city}: 暂时无法获取天气信息。")

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
            f"图文模式: {'开启' if self._rich_mode_enabled() else '关闭'}",
            f"TG 自动删命令: {'开启' if self._auto_delete_command_on_telegram() else '关闭'}",
            f"默认城市: {self._default_city() or '未设置'}",
            f"天气源: {self._weather_provider_label()}",
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
        async with self._http_client() as client:
            report_data = await self._collect_report_data_with_client(client, city)
            report_text = self._render_report_text(report_data)
            if not self._rich_mode_enabled():
                return {
                    "mode": "text",
                    "content": report_text,
                }

            try:
                await self._enrich_news_items_for_rich_mode(client, report_data["news"])
                page_url = await self._create_telegraph_page(
                    client,
                    title=self._telegraph_page_title(report_data["title"]),
                    content=self._build_report_telegraph_nodes(report_data),
                )
                return {
                    "mode": "text",
                    "content": self._telegraph_message(report_data["title"], page_url),
                }
            except Exception as exc:
                logger.exception("晨报图文页生成失败，已回退为文本模式: %s", exc)
                return {
                    "mode": "text",
                    "content": report_text,
                }

    async def _build_news_payload(self) -> dict[str, str]:
        async with self._http_client() as client:
            news_data = await self._collect_news_data_with_client(client)
            news_text = self._render_news_text(news_data)
            if not self._rich_mode_enabled():
                return {
                    "mode": "text",
                    "content": news_text,
                }

            try:
                await self._enrich_news_items_for_rich_mode(client, news_data["news"])
                page_url = await self._create_telegraph_page(
                    client,
                    title=self._telegraph_page_title(news_data["title"]),
                    content=self._build_news_telegraph_nodes(news_data),
                )
                return {
                    "mode": "text",
                    "content": self._telegraph_message(news_data["title"], page_url),
                }
            except Exception as exc:
                logger.exception("新闻图文页生成失败，已回退为文本模式: %s", exc)
                return {
                    "mode": "text",
                    "content": news_text,
                }

    def _build_message_chain(self, payload: dict[str, str]) -> MessageChain:
        return MessageChain().message(payload["content"])

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
            logger.warning(
                "Telegram 删除命令消息失败: chat_id=%s message_id=%s error=%s",
                chat_id,
                message_id,
                exc,
            )

    async def _build_report(self, city: str = "") -> str:
        async with self._http_client() as client:
            report_data = await self._collect_report_data_with_client(client, city)
        return self._render_report_text(report_data)

    async def _build_news_text(self) -> str:
        async with self._http_client() as client:
            news_data = await self._collect_news_data_with_client(client)
        return self._render_news_text(news_data)

    async def _collect_report_data_with_client(
        self, client: httpx.AsyncClient, city: str = ""
    ) -> dict[str, Any]:
        resolved_city = city.strip() or self._default_city()
        task_map: dict[str, asyncio.Task] = {}

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

        return {
            "title": str(self.config.get("report_title", "每日晨报") or "每日晨报"),
            "date_line": f"{now:%Y-%m-%d} 星期{WEEKDAY_CN[now.weekday()]}",
            "weather": self._result_or_none("weather", sections),
            "news": self._result_or_none("news", sections) or [],
            "quote": self._result_or_none("quote", sections),
            "poem": self._result_or_none("poem", sections),
        }

    async def _collect_news_data_with_client(
        self, client: httpx.AsyncClient
    ) -> dict[str, Any]:
        now = datetime.now(self._timezone())
        try:
            news = await self._fetch_headlines(client)
        except Exception as exc:
            logger.exception("新闻速览拉取失败: %s", exc)
            news = []

        return {
            "title": "新闻速览",
            "date_line": f"{now:%Y-%m-%d} 星期{WEEKDAY_CN[now.weekday()]}",
            "news": news,
        }

    def _result_or_none(self, key: str, results: dict[str, Any]) -> Any:
        value = results.get(key)
        if isinstance(value, Exception):
            logger.warning("晨报数据块拉取失败: %s error=%s", key, value)
            return None
        return value

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

    def _weather_provider(self) -> str:
        value = str(self.config.get("weather_provider", "uapi") or "").strip().lower()
        return value if value in {"uapi", "open-meteo", "custom"} else "uapi"

    def _weather_provider_label(self) -> str:
        provider = self._weather_provider()
        if provider == "custom":
            return "自定义 API"
        if provider == "uapi":
            return "UAPI"
        return "Open-Meteo"

    def _custom_weather_api_url(self) -> str:
        return str(self.config.get("custom_weather_api_url", "") or "").strip()

    def _custom_weather_response_path(self) -> str:
        return str(self.config.get("custom_weather_response_path", "") or "").strip()

    def _custom_weather_headers(self) -> dict[str, str]:
        raw = str(self.config.get("custom_weather_headers", "") or "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except Exception as exc:
            logger.warning("custom_weather_headers 解析失败: %s", exc)
            return {}
        if not isinstance(data, dict):
            logger.warning("custom_weather_headers 必须是 JSON 对象。")
            return {}
        return {
            str(key): str(value)
            for key, value in data.items()
            if key and value is not None
        }

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

    def _scheduler_config_key(self) -> str:
        return json.dumps(
            {
                "enabled": self._is_enabled(),
                "delivery_time": self._delivery_time_text(),
                "delivery_timezone": self._timezone_name(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

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

    def _rich_mode_enabled(self) -> bool:
        return bool(self.config.get("image_mode_enabled", False))

    def _bot_display_name(self) -> str:
        return str(self.config.get("bot_display_name", "") or "").strip()

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

    def _fill_url_template(self, template: str, values: dict[str, str]) -> str:
        return re.sub(
            r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}",
            lambda match: values.get(match.group(1), ""),
            template,
        )

    def _extract_data_by_path(self, data: Any, path: str) -> Any:
        current = data
        for segment in [part.strip() for part in path.split(".") if part.strip()]:
            if isinstance(current, dict):
                if segment not in current:
                    return None
                current = current[segment]
                continue
            if isinstance(current, list):
                try:
                    index = int(segment)
                except ValueError:
                    return None
                if index < 0 or index >= len(current):
                    return None
                current = current[index]
                continue
            return None
        return current

    def _guess_weather_text_from_json(self, data: Any) -> str:
        direct_text = self._text_value(data)
        if direct_text:
            return direct_text
        for path in (
            "weather",
            "summary",
            "text",
            "result",
            "message",
            "data.weather",
            "data.summary",
            "data.text",
            "data.result",
            "current.weather",
            "current.summary",
            "current.text",
        ):
            value = self._extract_data_by_path(data, path)
            text = self._text_value(value)
            if text:
                return text
        return ""

    def _text_value(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return self._clean_text(str(value))
        return ""

    def _safe_float(self, value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)

        text = self._clean_text(str(value))
        if not text:
            return None
        try:
            return float(text)
        except (TypeError, ValueError):
            logger.warning("天气数字字段解析失败: value=%r", value)
            return None

    def _safe_int(self, value: Any) -> int | None:
        numeric = self._safe_float(value)
        if numeric is None:
            return None
        return int(numeric)

    def _remember_geo_cache(self, cache_key: str, result: dict[str, Any]):
        self._geo_cache.pop(cache_key, None)
        self._geo_cache[cache_key] = result.copy()
        while len(self._geo_cache) > GEO_CACHE_MAX_SIZE:
            self._geo_cache.popitem(last=False)

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
