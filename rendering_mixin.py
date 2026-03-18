from __future__ import annotations

from datetime import datetime
from typing import Any

from daily_shared import WEEKDAY_CN


class RenderingMixin:
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
                nodes.extend(
                    self._build_news_telegraph_item_nodes(
                        remaining_news,
                        start_index=1 if lead_item else 0,
                        total_count=len(news),
                    )
                )

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
                nodes.extend(
                    self._build_news_telegraph_item_nodes(
                        remaining_news,
                        start_index=1 if lead_item else 0,
                        total_count=len(news),
                    )
                )
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
