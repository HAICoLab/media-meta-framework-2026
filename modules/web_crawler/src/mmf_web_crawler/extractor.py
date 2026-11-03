from __future__ import annotations

from datetime import date, datetime

import httpx
from bs4 import BeautifulSoup
from mmf_schema import BaseExtractor, MetadataRecord, SourceType

USER_AGENT = "media-meta-framework/0.1 (research crawler)"


def _meta(soup: BeautifulSoup, *, name: str | None = None, prop: str | None = None) -> str | None:
    attrs = {"name": name} if name else {"property": prop}
    tag = soup.find("meta", attrs=attrs)
    if tag and tag.get("content"):
        return str(tag["content"]).strip()
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_html(html: str, url: str) -> dict:
    """네트워크 없이 테스트할 수 있도록 파싱 로직만 분리한다."""
    soup = BeautifulSoup(html, "html.parser")
    og = {
        t["property"]: str(t.get("content", "")).strip()
        for t in soup.find_all("meta", attrs={"property": True})
        if str(t["property"]).startswith("og:")
    }
    canonical = soup.find("link", rel="canonical")
    keywords = _meta(soup, name="keywords")
    html_tag = soup.find("html")
    body_text = soup.get_text(" ", strip=True)
    return {
        "title": og.get("og:title")
        or (soup.title.string.strip() if soup.title and soup.title.string else None),
        "description": og.get("og:description") or _meta(soup, name="description"),
        "keywords": [k.strip() for k in keywords.split(",") if k.strip()] if keywords else [],
        "language": html_tag.get("lang") if html_tag else None,
        "published_at": _parse_date(_meta(soup, prop="article:published_time")),
        "publisher": og.get("og:site_name"),
        "authors": [a] if (a := _meta(soup, name="author")) else [],
        "text": body_text[:5000] or None,
        "extra": {
            "canonical_url": canonical.get("href") if canonical else url,
            "open_graph": og,
        },
    }


class WebExtractor(BaseExtractor):
    name = "web_crawler"
    version = "0.1.0"

    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def can_handle(self, source: str) -> bool:
        return source.startswith(("http://", "https://"))

    def fetch(self, url: str) -> str:
        resp = httpx.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=self.timeout, follow_redirects=True
        )
        resp.raise_for_status()
        return resp.text

    def extract(self, source: str) -> list[MetadataRecord]:
        fields = parse_html(self.fetch(source), source)
        return [
            MetadataRecord(
                id=MetadataRecord.make_id(SourceType.WEB, source),
                source_type=SourceType.WEB,
                provenance=self.provenance(source),
                **fields,
            )
        ]
