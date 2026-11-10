"""YouTube 공용 헬퍼 (영상 ID·제목/설명·썸네일). Playwright 없이 httpx 만 사용."""

from typing import Optional
import json
import logging
import re
from playwright.async_api import BrowserContext

logger = logging.getLogger(__name__)

def extract_youtube_id(src: str) -> Optional[str]:
    m = re.search(r"(?:youtube\.com/embed/|youtu\.be/|v=)([A-Za-z0-9_-]{11})", src)
    return m.group(1) if m else None


async def best_yt_thumbnail(video_id: str) -> str:
    """YouTube video_id에 대해 실제 존재하는 가장 좋은 화질의 썸네일 URL 반환.
    maxresdefault → sddefault → hqdefault → mqdefault 순으로 시도.
    """
    import httpx as _httpx
    _qualities = ["maxresdefault", "sddefault", "hqdefault", "mqdefault"]
    _base = f"https://img.youtube.com/vi/{video_id}"
    async with _httpx.AsyncClient(timeout=5, follow_redirects=False) as _c:
        for q in _qualities:
            url = f"{_base}/{q}.jpg"
            try:
                r = await _c.head(url)
                if r.status_code == 200:
                    return url
            except Exception:
                pass
    # 모두 실패하면 기본값
    return f"{_base}/hqdefault.jpg"


async def get_youtube_info(context: BrowserContext, video_id: str) -> dict:
    """YouTube 영상 제목과 상세 설명을 수집합니다 (httpx 기반, Playwright 불필요)."""
    import httpx as _httpx
    url = f"https://www.youtube.com/watch?v={video_id}"
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    try:
        async with _httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=15) as client:
            r = await client.get(url)
        text = r.text

        title, description = "", ""

        # 1) oEmbed API로 제목 수집 (가장 안정적)
        try:
            async with _httpx.AsyncClient(timeout=8) as c2:
                r2 = await c2.get(f"https://www.youtube.com/oembed?url={url}&format=json")
            if r2.status_code == 200:
                title = r2.json().get("title", "")
        except Exception:
            pass

        # 2) oEmbed 실패 시 og:title 메타 태그 fallback
        if not title:
            m = re.search(r'<meta\s+(?:name|property)="(?:og:title|title)"\s+content="([^"]+)"', text)
            if m:
                title = m.group(1)

        # 3) description: 전체 설명(shortDescription, JSON 이스케이프) 우선 — og:description 은 160자 안팎으로 잘린다
        sdm = re.search(r'"shortDescription":"((?:[^"\\]|\\.)*)"', text)
        if sdm:
            try:
                description = json.loads('"' + sdm.group(1) + '"')
            except Exception:
                description = ""

        # 3-2) og:description 메타 태그 (잘린 요약)
        if not description:
            dm = re.search(r'<meta\s+(?:name|property)="og:description"\s+content="([^"]*)"', text)
            if dm:
                description = dm.group(1)

        # 4) description fallback: ytInitialData attributedDescription
        if not description:
            dm2 = re.search(
                r'"attributedDescription"\s*:\s*\{"content"\s*:\s*"(.*?)",\s*"commandRuns"',
                text, re.DOTALL
            )
            if dm2:
                description = dm2.group(1).replace("\\n", "\n").replace('\\"', '"')

        return {"video_id": video_id, "youtube_url": url, "title": title, "description": description}
    except Exception as e:
        logger.warning(f"YouTube 정보 수집 실패 ({video_id}): {e}")
        return {"video_id": video_id, "youtube_url": url, "title": "", "description": ""}

