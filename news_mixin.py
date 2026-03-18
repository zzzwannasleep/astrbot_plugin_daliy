from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import feedparser
import httpx
from astrbot.api import logger


class NewsMixin:
    async def _fetch_headlines(self, client: httpx.AsyncClient) -> list[dict[str, str]]:
        cached = self._cached_news_items_from_memory()
        if cached is not None:
            return cached

        async with self._news_cache_lock:
            cached = self._cached_news_items_from_memory()
            if cached is not None:
                return cached

            persisted = await self.get_kv_data("daily_news_cache", {})
            cached = self._cached_news_items_from_entry(persisted)
            if cached is not None:
                self._news_cache = self._build_news_cache_entry(cached)
                return self._clone_news_items(cached)

            items = await self._fetch_headlines_uncached(client)
            await self._set_news_cache(items)
            return self._clone_news_items(items)

    async def _fetch_headlines_uncached(
        self, client: httpx.AsyncClient
    ) -> list[dict[str, str]]:
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

    async def _persist_news_cache(self, news: list[dict[str, str]]):
        async with self._news_cache_lock:
            await self._set_news_cache(news)

    async def _set_news_cache(self, news: list[dict[str, str]]):
        entry = self._build_news_cache_entry(news)
        self._news_cache = entry
        await self.put_kv_data("daily_news_cache", entry)

    def _build_news_cache_entry(self, news: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "date": self._news_cache_date(),
            "signature": self._news_cache_signature(),
            "items": self._normalize_news_items(news),
        }

    def _cached_news_items_from_memory(self) -> list[dict[str, str]] | None:
        return self._cached_news_items_from_entry(self._news_cache)

    def _cached_news_items_from_entry(self, entry: Any) -> list[dict[str, str]] | None:
        if not isinstance(entry, dict):
            return None
        if entry.get("date") != self._news_cache_date():
            return None
        if entry.get("signature") != self._news_cache_signature():
            return None
        return self._normalize_news_items(entry.get("items", []))

    def _normalize_news_items(self, items: Any) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        if not isinstance(items, list):
            return normalized

        for item in items:
            if not isinstance(item, dict):
                continue
            normalized_item = {
                "title": self._clip_text(self._clean_text(str(item.get("title", "") or "")), 80),
                "source": self._clean_text(str(item.get("source", "") or "")),
                "link": self._clean_text(str(item.get("link", "") or "")),
                "summary": self._clip_text(
                    self._clean_text(str(item.get("summary", "") or "")),
                    240,
                ),
                "image": self._clean_text(str(item.get("image", "") or "")),
            }
            if not normalized_item["title"] and not normalized_item["summary"]:
                continue
            normalized.append(normalized_item)

        return normalized[: self._news_limit()]

    @staticmethod
    def _clone_news_items(news: list[dict[str, str]]) -> list[dict[str, str]]:
        return [item.copy() for item in news]

    def _news_cache_date(self) -> str:
        return datetime.now(self._timezone()).date().isoformat()

    def _news_cache_signature(self) -> str:
        return json.dumps(
            {
                "rss_urls": self._rss_urls(),
                "news_limit": self._news_limit(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

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
