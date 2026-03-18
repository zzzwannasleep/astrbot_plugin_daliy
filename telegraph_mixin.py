from __future__ import annotations

import asyncio
import html
import json
import mimetypes
import os
import re
import tempfile
from contextlib import suppress
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from astrbot.api import logger


class TelegraphMixin:
    async def _enrich_news_items_for_rich_mode(
        self, client: httpx.AsyncClient, news: list[dict[str, str]]
    ):
        if not news:
            return
        await asyncio.gather(
            *(self._enrich_single_news_item(client, item) for item in news),
            return_exceptions=True,
        )
        await self._persist_news_cache(news)

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

        if item.get("image") and not self._is_telegraph_asset_url(item["image"]):
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

    async def _create_telegraph_page(
        self,
        client: httpx.AsyncClient,
        title: str,
        content: list[dict[str, Any]],
    ) -> str:
        for attempt in range(2):
            access_token = await self._get_telegraph_access_token(
                client,
                force_refresh=attempt > 0,
            )
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
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                if attempt == 0 and self._is_telegraph_token_error_response(response):
                    logger.warning("Telegraph access_token is invalid; clearing cache and retrying")
                    await self._clear_telegraph_access_token()
                    continue
                raise

            data = response.json()
            if data.get("ok"):
                return str(data["result"]["url"])

            error_message = str(data.get("error") or "Telegraph createPage failed")
            if attempt == 0 and self._is_telegraph_token_error_message(error_message):
                logger.warning(
                    "Telegraph access_token is invalid; clearing cache and retrying: %s",
                    error_message,
                )
                await self._clear_telegraph_access_token()
                continue
            raise RuntimeError(error_message)

        raise RuntimeError("Telegraph createPage failed after refreshing access token")

    async def _get_telegraph_access_token(
        self,
        client: httpx.AsyncClient,
        force_refresh: bool = False,
    ) -> str:
        if not force_refresh:
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

    async def _clear_telegraph_access_token(self):
        await self.put_kv_data("telegraph_access_token", "")

    def _is_telegraph_token_error_response(self, response: httpx.Response) -> bool:
        try:
            data = response.json()
        except ValueError:
            return self._is_telegraph_token_error_message(response.text)
        if not isinstance(data, dict):
            return False
        return self._is_telegraph_token_error_message(str(data.get("error", "") or ""))

    @staticmethod
    def _is_telegraph_token_error_message(message: str) -> bool:
        normalized = message.strip().upper()
        return "ACCESS_TOKEN" in normalized and any(
            keyword in normalized for keyword in ("INVALID", "REQUIRED", "EMPTY", "EXPIRED")
        )

    def _telegraph_author_name(self) -> str:
        return self._bot_display_name() or "AstrBot Daily"

    def _telegraph_author_url(self) -> str:
        return "https://github.com/zzzwannasleep/astrbot_plugin_daliy"

    @staticmethod
    def _is_telegraph_asset_url(url: str) -> bool:
        hostname = urlparse(url).hostname or ""
        return hostname.lower() in {"telegra.ph", "graph.org"}
