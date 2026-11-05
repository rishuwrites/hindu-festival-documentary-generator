from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from urllib.error import URLError
from urllib.parse import parse_qs, quote_plus, urlparse
from urllib.request import Request, urlopen


USER_AGENT = "FestivalDocumentaryResearch/1.0 (educational documentary; contact: project owner)"
LOGGER = logging.getLogger(__name__)


@dataclass
class Source:
    title: str
    url: str
    publisher: str
    snippet: str
    source_type: str
    retrieved_at: str
    text: str = ""
    published_at: str = ""


class _SearchParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._active: dict[str, str] | None = None
        self._capture: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "") or ""
        if tag == "a" and "result__a" in classes:
            self._active = {"title": "", "url": attrs_dict.get("href") or ""}
            self._capture = "title"
        elif tag in {"a", "div", "td"} and "result__snippet" in classes and self.results:
            self._capture = "snippet"

    def handle_data(self, data: str) -> None:
        if self._capture == "title" and self._active is not None:
            self._active["title"] += data
        elif self._capture == "snippet" and self.results:
            self.results[-1]["snippet"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._active is not None:
            self.results.append({**self._active, "snippet": ""})
            self._active = None
            self._capture = None
        elif tag in {"a", "div", "td"} and self._capture == "snippet":
            self._capture = None


class ResearchCollector:
    """Collects search results and page extracts; source quality is retained for review."""

    def __init__(
        self,
        timeout: int = 25,
        max_pages: int = 10,
        google_api_key: str = "",
        google_engine_id: str = "",
    ) -> None:
        self.timeout = timeout
        self.max_pages = max_pages
        self.google_api_key = google_api_key
        self.google_engine_id = google_engine_id

    def _get(self, url: str, accept: str = "text/html") -> bytes:
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
        with urlopen(request, timeout=self.timeout) as response:
            return response.read(2_000_000)

    def search(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        if self.google_api_key and self.google_engine_id:
            try:
                url = (
                    "https://www.googleapis.com/customsearch/v1?"
                    f"key={quote_plus(self.google_api_key)}&cx={quote_plus(self.google_engine_id)}"
                    f"&q={quote_plus(query)}&num={min(limit, 10)}"
                )
                payload = json.loads(self._get(url, "application/json"))
                results = [
                    {
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "snippet": item.get("snippet", ""),
                    }
                    for item in payload.get("items", [])
                    if item.get("link", "").startswith("http")
                ]
                if results:
                    return results
                LOGGER.info("Google CSE returned no results; using DuckDuckGo fallback.")
            except (URLError, TimeoutError, ValueError) as exc:
                LOGGER.warning("Google CSE failed; using DuckDuckGo fallback: %s", exc)
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        parser = _SearchParser()
        parser.feed(self._get(url).decode("utf-8", "replace"))
        results: list[dict[str, str]] = []
        for item in parser.results:
            href = html.unescape(item["url"])
            if href.startswith("//"):
                href = "https:" + href
            parsed = urlparse(href)
            if parsed.netloc.endswith("duckduckgo.com"):
                href = parse_qs(parsed.query).get("uddg", [href])[0]
            if not href.startswith("http"):
                continue
            results.append({**item, "url": href})
            if len(results) >= limit:
                break
        return results

    def _wikipedia(self, festival: str) -> list[dict[str, str]]:
        query = quote_plus(festival)
        url = (
            "https://en.wikipedia.org/w/api.php?action=query&generator=search"
            f"&gsrsearch={query}&gsrlimit=3&prop=extracts|info&inprop=url"
            "&exintro=1&explaintext=1&format=json"
        )
        try:
            payload = json.loads(self._get(url, "application/json"))
        except (URLError, TimeoutError, ValueError) as exc:
            LOGGER.warning("Wikipedia background lookup failed: %s", exc)
            return []
        pages = payload.get("query", {}).get("pages", {})
        return [
            {
                "title": page.get("title", "Wikipedia"),
                "url": page.get("fullurl", ""),
                "snippet": page.get("extract", "")[:1200],
                "text": page.get("extract", "")[:12000],
                "source_type": "background reference (Wikipedia)",
            }
            for page in pages.values()
        ]

    @staticmethod
    def _page_text(raw: bytes) -> tuple[str, str, str]:
        class TextParser(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.title_parts: list[str] = []
                self.text_parts: list[str] = []
                self.published_at = ""
                self._in_title = False
                self._skip = 0

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                attrs_dict = {key.lower(): value or "" for key, value in attrs}
                if tag == "title":
                    self._in_title = True
                if tag == "meta":
                    field = (
                        attrs_dict.get("property", "")
                        or attrs_dict.get("name", "")
                        or attrs_dict.get("itemprop", "")
                    ).lower()
                    if field in {"article:published_time", "datepublished", "pubdate", "date"}:
                        self.published_at = attrs_dict.get("content", "")
                if tag in {"script", "style", "noscript", "svg"}:
                    self._skip += 1

            def handle_endtag(self, tag: str) -> None:
                if tag == "title":
                    self._in_title = False
                if tag in {"script", "style", "noscript", "svg"} and self._skip:
                    self._skip -= 1

            def handle_data(self, data: str) -> None:
                value = re.sub(r"\s+", " ", data).strip()
                if not value:
                    return
                if self._in_title:
                    self.title_parts.append(value)
                elif not self._skip:
                    self.text_parts.append(value)

        parser = TextParser()
        parser.feed(raw.decode("utf-8", "replace"))
        return " ".join(parser.title_parts), " ".join(parser.text_parts), parser.published_at

    @staticmethod
    def _source_type(host: str) -> str:
        host = host.lower().removeprefix("www.")
        if host.endswith(".gov.in") or host == "gov.in":
            return "government"
        if host.endswith(".edu") or host.endswith(".ac.in"):
            return "academic"
        if "wikipedia.org" in host:
            return "background reference (Wikipedia)"
        return "web publication; verify authority"

    def collect(self, festival: str) -> list[Source]:
        queries = [
            f"{festival} history significance official India",
            f"{festival} rituals regional traditions government tourism",
            f"{festival} scripture historical academic",
            f"{festival} festival culture India official",
        ]
        found: dict[str, dict[str, str]] = {}
        for query in queries:
            try:
                for item in self.search(query, limit=8):
                    host = urlparse(item["url"]).netloc.lower()
                    if "duckduckgo.com" not in host:
                        found.setdefault(item["url"].split("#")[0], item)
            except (URLError, TimeoutError, ValueError) as exc:
                LOGGER.warning("Research search failed for %r: %s", query, exc)
                continue

        pages: list[Source] = []
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for item in list(found.values())[: self.max_pages]:
            url = item["url"]
            try:
                page_title, text, published_at = self._page_text(self._get(url))
            except (URLError, TimeoutError, ValueError) as exc:
                LOGGER.warning("Could not retrieve research page %s: %s", url, exc)
                page_title, text, published_at = "", "", ""
            host = urlparse(url).netloc
            pages.append(
                Source(
                    title=page_title or item.get("title", url),
                    url=url,
                    publisher=host,
                    snippet=(item.get("snippet", "") or text[:500])[:1200],
                    source_type=self._source_type(host),
                    retrieved_at=now,
                    text=text[:12000],
                    published_at=published_at,
                )
            )
        for item in self._wikipedia(festival):
            pages.append(
                Source(
                    title=item["title"],
                    url=item["url"],
                    publisher="Wikipedia",
                    snippet=item["snippet"],
                    source_type=item["source_type"],
                    retrieved_at=now,
                    text=item["text"],
                )
            )
        unique: dict[str, Source] = {}
        for source in pages:
            if source.url:
                unique.setdefault(source.url, source)
        return list(unique.values())

    @staticmethod
    def save(sources: list[Source], json_path: str, markdown_path: str) -> None:
        from pathlib import Path

        Path(json_path).write_text(
            json.dumps([asdict(source) for source in sources], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        lines = ["# Research sources", ""]
        for index, source in enumerate(sources, 1):
            lines.extend(
                [
                    f"## {index}. {source.title}",
                    f"- Publisher: {source.publisher}",
                    f"- Type: {source.source_type}",
                    f"- Retrieved: {source.retrieved_at}",
                    f"- Published: {source.published_at or 'not stated by page metadata'}",
                    f"- URL: {source.url}",
                    "",
                    source.snippet,
                    "",
                ]
            )
        Path(markdown_path).write_text("\n".join(lines), encoding="utf-8")
