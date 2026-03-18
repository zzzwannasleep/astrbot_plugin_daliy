from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import os
import re
import tempfile
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse
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
            logger.warning("Telegram 删除命令消息失败: chat_id=%s message_id=%s error=%s", chat_id, message_id, exc)

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

    def _render_report_text(self, report_data: dict[str, Any]) -> str:
        lines = [
            report_data["title"],
            report_data["date_line"],
        ]

        if report_data.get("weather"):
            lines.extend(["", "天气", report_data["weather"]])

        news = report_data.get("news") or []
        if news:
            lines.extend(["", "新闻速览"])
            self._append_news_lines(lines, news)

        if report_data.get("quote"):
            lines.extend(["", "今日一句", report_data["quote"]])

        if report_data.get("poem"):
            lines.extend(["", "诗词", report_data["poem"]])

        footer = self._footer_text()
        if footer:
            lines.extend(["", footer])

        if len(lines) <= 2:
            lines.extend(["", "今天的外部数据暂时拉取失败，请检查网络、RSS 源或接口配置。"])

        return "\n".join(lines)

    def _render_news_text(self, news_data: dict[str, Any]) -> str:
        lines = [
            news_data["title"],
            news_data["date_line"],
        ]

        news = news_data.get("news") or []
        if news:
            lines.append("")
            self._append_news_lines(lines, news)
        else:
            lines.extend(["", "当前没有可用新闻，请检查 RSS 源或接口配置。"])

        footer = self._footer_text()
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
        footer = self._footer_text()
        if footer:
            lines.extend(["", footer])
        return "\n".join(lines)

    def _append_news_lines(self, lines: list[str], news: list[dict[str, str]]):
        for index, item in enumerate(news):
            title = item.get("title", "").strip()
            summary = self._clip_text(item.get("summary", "").strip(), 140)
            link = item.get("link", "").strip()
            content = summary or title
            if not content:
                continue

            if index > 0:
                lines.append("")

            lines.append(content)
            if link:
                lines.append(f"- [来源]({link})")
            else:
                lines.append("- 来源")

    def _footer_text(self) -> str:
        bot_name = self._bot_display_name()
        if bot_name:
            return f"由 {bot_name} 推送"
        return str(self.config.get("footer", "") or "").strip()

    def _telegraph_page_title(self, title: str) -> str:
        today = datetime.now(self._timezone()).strftime("%Y-%m-%d")
        return f"{title} {today}"

    def _telegraph_message(self, title: str, page_url: str) -> str:
        return "\n".join(
            [
                f"{title}（图文版）",
                page_url,
            ]
        )

    def _build_report_telegraph_nodes(
        self, report_data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []

        news = report_data.get("news") or []
        lead_item = self._lead_news_item(news)
        hero_image = lead_item.get("image", "").strip() if lead_item else self._first_news_image(news)
        if hero_image:
            nodes.append(self._telegraph_image_node(hero_image, report_data["title"]))

        nodes.append({"tag": "p", "children": [report_data["date_line"]]})

        if report_data.get("weather"):
            nodes.extend(
                [
                    {"tag": "h4", "children": ["天气"]},
                    {"tag": "p", "children": [report_data["weather"]]},
                ]
            )

        if news:
            nodes.append({"tag": "h4", "children": ["新闻速览"]})
            if lead_item:
                nodes.extend(self._build_lead_telegraph_nodes(lead_item, len(news)))
            remaining_news = news[1:] if lead_item else news
            if remaining_news:
                if lead_item:
                    nodes.append({"tag": "h4", "children": ["更多要闻"]})
                nodes.extend(self._build_news_telegraph_item_nodes(remaining_news, start_index=1 if lead_item else 0, total_count=len(news)))

        if report_data.get("quote"):
            nodes.extend(
                [
                    {"tag": "h4", "children": ["今日一句"]},
                    {"tag": "blockquote", "children": [report_data["quote"]]},
                ]
            )

        if report_data.get("poem"):
            nodes.extend(
                [
                    {"tag": "h4", "children": ["诗词"]},
                    {"tag": "blockquote", "children": [report_data["poem"]]},
                ]
            )

        footer = self._footer_text()
        if footer:
            nodes.append({"tag": "p", "children": [footer]})

        return nodes

    def _build_news_telegraph_nodes(
        self, news_data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        news = news_data.get("news") or []
        lead_item = self._lead_news_item(news)
        hero_image = lead_item.get("image", "").strip() if lead_item else self._first_news_image(news)
        if hero_image:
            nodes.append(self._telegraph_image_node(hero_image, news_data["title"]))

        nodes.append({"tag": "p", "children": [news_data["date_line"]]})
        if news:
            if lead_item:
                nodes.extend(self._build_lead_telegraph_nodes(lead_item, len(news)))
            remaining_news = news[1:] if lead_item else news
            if remaining_news:
                if lead_item:
                    nodes.append({"tag": "h4", "children": ["更多要闻"]})
                nodes.extend(self._build_news_telegraph_item_nodes(remaining_news, start_index=1 if lead_item else 0, total_count=len(news)))
        else:
            nodes.append({"tag": "p", "children": ["当前没有可用新闻，请检查 RSS 源或接口配置。"]})

        footer = self._footer_text()
        if footer:
            nodes.append({"tag": "p", "children": [footer]})
        return nodes

    def _build_lead_telegraph_nodes(
        self, item: dict[str, str], total_count: int
    ) -> list[dict[str, Any]]:
        title = item.get("title", "").strip()
        link = item.get("link", "").strip()
        summary = self._summary_for_rich_mode(item, 0, total_count, is_lead=True)
        nodes: list[dict[str, Any]] = []

        if title:
            nodes.append({"tag": "h3", "children": [title]})
        if summary:
            nodes.append({"tag": "p", "children": [summary]})
        if link:
            nodes.append(
                {
                    "tag": "aside",
                    "children": self._news_link_children(link),
                }
            )
        nodes.append({"tag": "hr"})
        return nodes

    def _build_news_telegraph_item_nodes(
        self,
        news: list[dict[str, str]],
        start_index: int = 0,
        total_count: int | None = None,
    ) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        total = total_count if total_count is not None else len(news)
        for offset, item in enumerate(news):
            item_index = start_index + offset
            title = item.get("title", "").strip()
            link = item.get("link", "").strip()
            summary = self._summary_for_rich_mode(item, item_index, total, is_lead=False)
            image = item.get("image", "").strip()
            if not title:
                continue

            nodes.append({"tag": "h4", "children": [title]})
            if image:
                nodes.append(self._telegraph_image_node(image, title))
            if summary:
                nodes.append({"tag": "p", "children": [summary]})
            if link:
                nodes.append(
                    {
                        "tag": "aside",
                        "children": self._news_link_children(link),
                    }
                )
            else:
                nodes.append({"tag": "aside", "children": ["来源"]})
            if offset < len(news) - 1:
                nodes.append({"tag": "hr"})
        return nodes

    def _summary_for_rich_mode(
        self, item: dict[str, str], index: int, total_count: int, is_lead: bool
    ) -> str:
        summary = item.get("summary", "").strip()
        if not summary:
            return ""

        if is_lead:
            limit = 260 if total_count <= 3 else 220 if total_count <= 5 else 180
        else:
            limit = 180 if total_count <= 3 else 140 if total_count <= 5 else 110
            if index >= 3:
                limit = min(limit, 100)
        return self._clip_text(summary, limit)

    def _news_link_children(self, link: str) -> list[Any]:
        return [
            "- ",
            {
                "tag": "a",
                "attrs": {"href": link},
                "children": ["来源"],
            },
            "  |  ",
            {
                "tag": "strong",
                "children": [
                    {
                        "tag": "a",
                        "attrs": {"href": link},
                        "children": ["阅读全文"],
                    }
                ],
            },
        ]

    async def _enrich_news_items_for_rich_mode(
        self, client: httpx.AsyncClient, news: list[dict[str, str]]
    ):
        if not news:
            return
        await asyncio.gather(
            *(self._enrich_single_news_item(client, item) for item in news),
            return_exceptions=True,
        )

    async def _enrich_single_news_item(
        self, client: httpx.AsyncClient, item: dict[str, str]
    ):
        link = item.get("link", "").strip()
        if link and (not item.get("image") or not item.get("summary")):
            try:
                preview = await self._fetch_article_preview(client, link)
            except Exception as exc:
                logger.warning("新闻详情抓取失败: link=%s error=%s", link, exc)
            else:
                if preview.get("image") and not item.get("image"):
                    item["image"] = preview["image"]
                if preview.get("summary") and not item.get("summary"):
                    item["summary"] = preview["summary"]

        if item.get("image"):
            uploaded_image = await self._upload_image_to_telegraph_via_tempfile(client, item["image"])
            if uploaded_image:
                item["image"] = uploaded_image
            else:
                item["image"] = ""

    async def _fetch_article_preview(
        self, client: httpx.AsyncClient, link: str
    ) -> dict[str, str]:
        response = await client.get(link)
        response.raise_for_status()
        html_text = response.text

        image = (
            self._extract_meta_content(html_text, "property", "og:image")
            or self._extract_meta_content(html_text, "name", "twitter:image")
            or self._extract_first_image_from_html(html_text)
        )
        summary = (
            self._extract_meta_content(html_text, "property", "og:description")
            or self._extract_meta_content(html_text, "name", "description")
            or self._extract_paragraph_summary_from_html(html_text)
        )

        return {
            "image": self._absolute_url(link, image.strip()),
            "summary": self._clip_text(self._clean_text(summary), 240) if summary else "",
        }

    async def _upload_image_to_telegraph_via_tempfile(
        self, client: httpx.AsyncClient, image_url: str
    ) -> str:
        image_url = image_url.strip()
        if not image_url:
            return ""
        if image_url.startswith("https://telegra.ph/file/"):
            return image_url

        temp_path = ""
        try:
            content_type = ""
            async with client.stream("GET", image_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                file_suffix = self._guess_image_suffix(image_url, content_type)
                with tempfile.NamedTemporaryFile(delete=False, suffix=file_suffix) as temp_file:
                    temp_path = temp_file.name
                    async for chunk in response.aiter_bytes():
                        temp_file.write(chunk)

            with open(temp_path, "rb") as image_file:
                upload_response = await client.post(
                    "https://telegra.ph/upload",
                    files={
                        "file": (
                            os.path.basename(temp_path),
                            image_file,
                            content_type or "application/octet-stream",
                        )
                    },
                )
            upload_response.raise_for_status()
            data = upload_response.json()
            if not isinstance(data, list) or not data or "src" not in data[0]:
                raise RuntimeError(f"Telegraph upload failed: {data}")
            return f"https://telegra.ph{data[0]['src']}"
        except Exception as exc:
            logger.warning("Telegraph 图片上传失败: url=%s error=%s", image_url, exc)
            return ""
        finally:
            if temp_path:
                with suppress(Exception):
                    os.remove(temp_path)

    @staticmethod
    def _guess_image_suffix(image_url: str, content_type: str) -> str:
        suffix = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
        if suffix:
            return suffix

        parsed = urlparse(image_url)
        filename = os.path.basename(parsed.path)
        _, ext = os.path.splitext(filename)
        if ext:
            return ext
        return ".jpg"

    @staticmethod
    def _absolute_url(base_url: str, maybe_relative_url: str) -> str:
        value = maybe_relative_url.strip()
        if not value:
            return ""
        return urljoin(base_url, value)

    @staticmethod
    def _extract_meta_content(html_text: str, attr_name: str, attr_value: str) -> str:
        pattern = (
            rf"<meta[^>]+{attr_name}=[\"']{re.escape(attr_value)}[\"'][^>]+content=[\"']([^\"']+)[\"']"
        )
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1))

        pattern = (
            rf"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+{attr_name}=[\"']{re.escape(attr_value)}[\"']"
        )
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1))
        return ""

    @staticmethod
    def _extract_first_image_from_html(html_text: str) -> str:
        match = re.search(r"<img[^>]+src=[\"']([^\"']+)[\"']", html_text, flags=re.IGNORECASE)
        if match:
            return html.unescape(match.group(1))
        return ""

    def _extract_paragraph_summary_from_html(self, html_text: str) -> str:
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", html_text, flags=re.IGNORECASE | re.DOTALL)
        cleaned: list[str] = []
        for paragraph in paragraphs:
            text = self._clean_html_text(paragraph)
            if len(text) < 20:
                continue
            cleaned.append(text)
            if len(" ".join(cleaned)) >= 220:
                break
        return self._clip_text(" ".join(cleaned), 240) if cleaned else ""

    @staticmethod
    def _clean_html_text(raw_html: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return " ".join(html.unescape(text).split())

    @staticmethod
    def _telegraph_image_node(image_url: str, caption: str = "") -> dict[str, Any]:
        children: list[Any] = [
            {
                "tag": "img",
                "attrs": {"src": image_url},
            }
        ]
        if caption:
            children.append({"tag": "figcaption", "children": [caption]})
        return {
            "tag": "figure",
            "children": children,
        }

    @staticmethod
    def _first_news_image(news: list[dict[str, str]]) -> str:
        for item in news:
            image = item.get("image", "").strip()
            if image:
                return image
        return ""

    @staticmethod
    def _lead_news_item(news: list[dict[str, str]]) -> dict[str, str] | None:
        for item in news:
            if item.get("title", "").strip():
                return item
        return None

    async def _create_telegraph_page(
        self,
        client: httpx.AsyncClient,
        title: str,
        content: list[dict[str, Any]],
    ) -> str:
        access_token = await self._get_telegraph_access_token(client)
        response = await client.post(
            "https://api.telegra.ph/createPage",
            data={
                "access_token": access_token,
                "title": title,
                "author_name": self._telegraph_author_name(),
                "author_url": self._telegraph_author_url(),
                "content": json.dumps(content, ensure_ascii=False),
                "return_content": "false",
            },
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Telegraph createPage failed")
        return str(data["result"]["url"])

    async def _get_telegraph_access_token(self, client: httpx.AsyncClient) -> str:
        token = str(await self.get_kv_data("telegraph_access_token", "") or "").strip()
        if token:
            return token

        response = await client.post(
            "https://api.telegra.ph/createAccount",
            data={
                "short_name": "astrbot_daliy",
                "author_name": self._telegraph_author_name(),
                "author_url": self._telegraph_author_url(),
            },
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "Telegraph createAccount failed")

        token = str(data["result"]["access_token"])
        await self.put_kv_data("telegraph_access_token", token)
        return token

    def _telegraph_author_name(self) -> str:
        return self._bot_display_name() or "AstrBot Daily"

    def _telegraph_author_url(self) -> str:
        return "https://github.com/zzzwannasleep/astrbot_plugin_daliy"

    def _result_or_none(self, key: str, results: dict[str, Any]) -> Any:
        value = results.get(key)
        if isinstance(value, Exception):
            logger.warning("晨报数据块拉取失败: %s error=%s", key, value)
            return None
        return value

    async def _fetch_weather_summary(
        self, client: httpx.AsyncClient, city_name: str
    ) -> str | None:
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
                        "link": self._clean_text(entry.get("link", "") or ""),
                        "summary": self._extract_entry_summary(entry),
                        "image": self._absolute_url(
                            self._clean_text(entry.get("link", "") or ""),
                            self._extract_entry_image(entry),
                        ),
                    }
                )
                if len(items) >= news_limit:
                    return items

        return items

    def _extract_entry_summary(self, entry: Any) -> str:
        candidates: list[str] = []
        for key in ("summary", "description"):
            value = entry.get(key, "")
            if value:
                candidates.append(str(value))

        for content_item in entry.get("content", []) or []:
            value = content_item.get("value", "")
            if value:
                candidates.append(str(value))

        for candidate in candidates:
            text = self._clean_html_text(candidate)
            if text:
                return self._clip_text(text, 240)
        return ""

    def _extract_entry_image(self, entry: Any) -> str:
        for media_item in entry.get("media_content", []) or []:
            url = media_item.get("url", "")
            if url:
                return self._clean_text(str(url))

        for media_item in entry.get("media_thumbnail", []) or []:
            url = media_item.get("url", "")
            if url:
                return self._clean_text(str(url))

        for link_item in entry.get("links", []) or []:
            link_type = str(link_item.get("type", "") or "")
            href = str(link_item.get("href", "") or "")
            if href and link_type.startswith("image/"):
                return self._clean_text(href)

        for key in ("summary", "description"):
            value = entry.get(key, "")
            if value:
                image = self._extract_first_image_from_html(str(value))
                if image:
                    return self._clean_text(image)

        for content_item in entry.get("content", []) or []:
            value = content_item.get("value", "")
            if value:
                image = self._extract_first_image_from_html(str(value))
                if image:
                    return self._clean_text(image)

        return ""

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

    def _weather_provider(self) -> str:
        value = str(self.config.get("weather_provider", "open-meteo") or "").strip().lower()
        return value if value in {"open-meteo", "custom"} else "open-meteo"

    def _weather_provider_label(self) -> str:
        if self._weather_provider() == "custom":
            return "自定义 API"
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
