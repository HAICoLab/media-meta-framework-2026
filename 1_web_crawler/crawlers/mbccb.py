"""
MBC충북 (news.mbccb.co.kr) 크롤러
- 뉴스 기사: 목록 페이지 → 기사 본문 수집
- 다시보기: 목록 페이지 → YouTube iframe → 제목/설명 수집
- 뉴스 LIVE: YouTube 채널의 라이브 스트림 정보 수집
"""

import asyncio
import logging
import re
from typing import Optional
from playwright.async_api import BrowserContext

from crawlers.youtube import extract_youtube_id as _extract_youtube_id, get_youtube_info, best_yt_thumbnail

logger = logging.getLogger(__name__)

MBCCB_NEWS_LIST_URL = "https://news.mbccb.co.kr/home/sub.php?menukey=61"
MBCCB_VOD_LIST_URL  = "https://news.mbccb.co.kr/home/sub.php?menukey=69"
MBCCB_LIVE_STREAMS_URL = "https://www.youtube.com/@mbcnews2484/streams"  # MBC뉴스 라이브 아카이브


def _yt_walk_ids(obj, out: list) -> None:
    """YouTube JSON 트리에서 videoId(11자) 를 등장 순서대로 수집."""
    if isinstance(obj, dict):
        v = obj.get("videoId")
        if isinstance(v, str) and len(v) == 11:
            out.append(v)
        for x in obj.values():
            _yt_walk_ids(x, out)
    elif isinstance(obj, list):
        for x in obj:
            _yt_walk_ids(x, out)


def _yt_walk_continuations(obj, out: list) -> None:
    """목록 끝의 '더 불러오기'(continuationItemRenderer) 토큰 수집. 정렬 칩 등 다른 토큰은 제외."""
    if isinstance(obj, dict):
        cir = obj.get("continuationItemRenderer")
        if isinstance(cir, dict):
            tok = (((cir.get("continuationEndpoint") or {}).get("continuationCommand")) or {}).get("token")
            if tok:
                out.append(tok)
        for x in obj.values():
            _yt_walk_continuations(x, out)
    elif isinstance(obj, list):
        for x in obj:
            _yt_walk_continuations(x, out)


async def _live_expand_ids(html: str, ids: list[str], max_count: int, existing: set | None,
                           headers: dict, max_pages: int = 60) -> list[str]:
    """/streams 첫 화면(약 30개) 이후를 YouTube 내부 browse API 의 continuation 으로 이어서 읽는다.
    '기존에 없는 신규 ID' 가 max_count 개가 될 때까지(또는 더 없을 때까지) 반복."""
    import httpx as _httpx
    import json as _json

    def _new_cnt() -> int:
        return sum(1 for v in ids if not existing or v not in existing)

    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", html, re.S)
    if not (key and ver and m):
        return ids
    def _grid_token(obj) -> str | None:
        """첫 화면에는 다른 패널의 continuation 도 섞여 있으므로 영상 그리드(richGridRenderer) 안의 것만 쓴다."""
        if isinstance(obj, dict):
            grid = obj.get("richGridRenderer")
            if isinstance(grid, dict):
                t: list[str] = []
                _yt_walk_continuations(grid, t)
                if t:
                    return t[-1]
            for x in obj.values():
                r = _grid_token(x)
                if r:
                    return r
        elif isinstance(obj, list):
            for x in obj:
                r = _grid_token(x)
                if r:
                    return r
        return None

    try:
        token = _grid_token(_json.loads(m.group(1)))
    except Exception:
        return ids
    seen = set(ids)
    async with _httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=25) as client:
        for page_no in range(max_pages):
            if not token or _new_cnt() >= max_count:
                break
            try:
                r = await client.post(
                    f"https://www.youtube.com/youtubei/v1/browse?key={key.group(1)}",
                    json={"context": {"client": {"clientName": "WEB", "clientVersion": ver.group(1),
                                                 "hl": "ko", "gl": "KR"}},
                          "continuation": token})
                if r.status_code != 200:
                    logger.warning(f"[MBCCB LIVE] continuation 응답 {r.status_code} → 중단")
                    break
                j = r.json()
            except Exception as e:
                logger.warning(f"[MBCCB LIVE] continuation 실패 → 중단: {e}")
                break
            got: list[str] = []
            _yt_walk_ids(j, got)
            added = [v for v in dict.fromkeys(got) if v not in seen]
            seen.update(added)
            ids.extend(added)
            nt: list[str] = []
            _yt_walk_continuations(j, nt)
            token = nt[-1] if nt else None
            logger.info(f"[MBCCB LIVE] 이어 읽기 {page_no + 1}: +{len(added)}개 (누적 {len(ids)}, 신규 {_new_cnt()})")
            if not added:
                break
            await asyncio.sleep(0.3)
    return ids


async def get_youtube_channel_live(context: BrowserContext, channel_id: str = None, max_count: int = 50, on_progress=None,
                                   existing_video_ids: set = None) -> list[dict]:
    """YouTube 채널의 라이브 방송 아카이브(/streams 탭) 영상 목록을 수집합니다.
    httpx로 HTML을 가져와 ytInitialData에서 videoId를 추출합니다."""
    import httpx as _httpx

    url = MBCCB_LIVE_STREAMS_URL
    results = []
    video_ids: list[str] = []

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    try:
        # ── httpx로 /streams 페이지 HTML 수신 ───────────────────────────────
        async with _httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=20) as client:
            resp = await client.get(url)
        html = resp.text

        # ytInitialData에서 videoId 추출 (인라인 JSON)
        video_ids_raw: list[str] = list(dict.fromkeys(
            re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
        ))
        logger.info(f"[MBCCB LIVE] 페이지에서 video_id {len(video_ids_raw)}개 발견")

        if not video_ids_raw:
            # Playwright 폴백: JS 렌더링이 필요한 경우
            logger.info("[MBCCB LIVE] httpx 추출 실패 → Playwright 폴백")
            page = await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(4000)
                for _ in range(max(1, max_count // 10)):
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(1500)
                video_ids_raw = await page.evaluate("""() => {
                    const m = document.body.innerHTML.match(/"videoId":"([A-Za-z0-9_-]{11})"/g) || [];
                    return [...new Set(m.map(x => x.match(/"videoId":"([A-Za-z0-9_-]{11})"/)[1]))];
                }""")
                logger.info(f"[MBCCB LIVE] Playwright 폴백: {len(video_ids_raw)}개 발견")
            finally:
                await page.close()

        # 첫 화면(약 30개)만으로 부족하면 '더 불러오기' 로 이어서 읽는다
        video_ids = list(dict.fromkeys(video_ids_raw))
        _new = [v for v in video_ids if not existing_video_ids or v not in existing_video_ids]
        if len(_new) < max_count:
            video_ids = await _live_expand_ids(html, video_ids, max_count, existing_video_ids, _HEADERS)

        # 기존 ID 제외 후 신규 max_count 개만 수집
        before = len(video_ids)
        if existing_video_ids:
            video_ids = [v for v in video_ids if v not in existing_video_ids]
            if before != len(video_ids):
                logger.info(f"[MBCCB LIVE] 기존 {before - len(video_ids)}개 건너뜀 → 신규 {len(video_ids)}개")
        video_ids = video_ids[:max_count]

        total = len(video_ids)
        logger.info(f"[MBCCB LIVE] 신규 수집 대상 {total}개")
        if on_progress: on_progress(0, total, f"신규 영상 {total}개")

        for i, vid in enumerate(video_ids):
            info = await get_youtube_info(context, vid)
            info["video_id"] = vid                                         # 반드시 포함
            info["youtube_url"] = f"https://www.youtube.com/watch?v={vid}"
            info["category"] = "live"
            info["source"] = "mbccb"
            results.append(info)
            if on_progress: on_progress(i + 1, total, info.get("title", vid)[:40])
            await asyncio.sleep(0.3)

        if on_progress: on_progress(total, total, f"완료 ({total}개)")

    except Exception as e:
        logger.error(f"[MBCCB] LIVE 채널 수집 실패: {e}", exc_info=True)

    return results


# ── 뉴스 기사 수집 ───────────────────────────────────────────────────────────

_NEWS_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}
_SKIP_IMG_RE = re.compile(
    r"icon|ico|logo|comment|banner|btn|button|arrow|bg_|sprite|common"
    r"|naver|kakao|popup|sc52_video|live_con|\.gif($|\?)",
    re.IGNORECASE,
)
_YT_RE = re.compile(
    r"(?:youtube\.com/(?:embed|watch|v|shorts)/|youtu\.be/)([A-Za-z0-9_-]{11})"
)


async def crawl_news_article(context: BrowserContext, article_url: str) -> dict:
    """개별 뉴스 기사 본문을 수집합니다 (httpx + BeautifulSoup 기반).
    Playwright 브라우저 없이 정적 HTML을 파싱해 안정적으로 동작합니다."""
    import httpx as _httpx
    from bs4 import BeautifulSoup as _BS

    try:
        async with _httpx.AsyncClient(headers=_NEWS_HTTP_HEADERS, follow_redirects=True, timeout=15) as client:
            r = await client.get(article_url)
        soup = _BS(r.text, "html.parser")

        # ── 제목: 기사 본문 h2 (메뉴 제외) ──────────────────────────────────
        SKIP_H2 = {"전체메뉴", "통합검색", "뉴스", "라디오", "TV", "홈"}
        title = ""
        for h2 in soup.find_all("h2"):
            t = h2.get_text(strip=True)
            if t and t not in SKIP_H2 and len(t) > 5:
                title = t
                break

        # ── YouTube 영상 감지 (content_el 정제 이전에 수행 — decompose 전에 수집) ──
        video_id = ""
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            m = _YT_RE.search(src)
            if m:
                video_id = m.group(1)
                break
        if not video_id:
            for a in soup.find_all("a", href=True):
                m = _YT_RE.search(a["href"])
                if m:
                    video_id = m.group(1)
                    break
        if not video_id:
            # 스크립트 내 유튜브 ID 탐색
            for sc in soup.find_all("script"):
                m = _YT_RE.search(sc.get_text())
                if m:
                    video_id = m.group(1)
                    break

        # ── 본문 영역 ─────────────────────────────────────────────────────────
        content_el = soup.select_one("[class*=content]")
        if content_el:
            # 소셜 공유 레이어, iframe, 스크립트 제거
            for noise in content_el.select(
                '.layer_share, [class*=share], [class*=sns], iframe, script, style'
            ):
                noise.decompose()
        raw_text = content_el.get_text(separator="\n") if content_el else soup.get_text(separator="\n")

        # 기자 + 날짜
        byline_m = re.search(
            r"([가-힣]{2,5})\s*기자\s*(?:송고\s*:)?\s*(\d{4}-\d{2}-\d{2}[\s\d:]*)",
            raw_text,
        )
        reporter = byline_m.group(1) if byline_m else ""
        date = byline_m.group(2).strip() if byline_m else ""

        # 본문: byline 이후 텍스트
        body = ""
        if content_el:
            if byline_m:
                idx = raw_text.find(byline_m.group(0))
                body = raw_text[idx + len(byline_m.group(0)):].strip() if idx >= 0 else raw_text.strip()
            elif title:
                idx = raw_text.find(title)
                body = raw_text[idx + len(title):].strip() if idx >= 0 else raw_text.strip()
            else:
                body = raw_text.strip()

        # ── 본문 전처리: \xa0 → 공백, 줄바꿈 정규화 ──────────────────────────
        body = body.replace('\xa0', ' ')
        body = body.replace('\r\n', '\n').replace('\r', '\n')  # CRLF → LF
        body = re.sub(r'[ \t]+', ' ', body)          # 연속 공백 → 1칸
        body = re.sub(r'\n{3,}', '\n\n', body)       # 3줄 이상 빈줄 → 2줄

        # ── 유튜브 자막 안내 문구 제거 ────────────────────────────────────────
        # 전체 문구: "※ 유튜브 자동자막(아이콘)은 ON/OFF 가능합니다."
        # 아이콘 문자가 \n으로 렌더링되면 여러 줄로 분리될 수 있음:
        #   줄1: "※"
        #   줄2: " 유튜브 자동자막("
        #   줄3: ")은 OFF 가능합니다."
        # → 처음 최대 6줄 안에 있는 자막 안내 패턴 줄을 모두 제거
        _lines = body.split('\n')
        _cut = 0
        for _i, _ln in enumerate(_lines[:6]):
            _s = _ln.strip()
            if re.search(r'유튜브|가능합니다|(?:ON|OFF|on/off)\s*가능', _s, re.IGNORECASE):
                _cut = _i + 1
            elif _s and len(_s) > 5 and _cut > 0:
                break  # 실제 기사 내용이 시작됐으므로 중단
        if _cut > 0:
            body = '\n'.join(_lines[_cut:]).lstrip('\n').strip()

        # ── 불필요한 꼬리 제거 ───────────────────────────────────────────────
        CUT_MARKERS = [
            "MBC충북 제보하기", "TV뉴스 시간 안내",
            "당신의 의견을 남겨주세요", "관련뉴스", "목록\n닉네임",
        ]
        for marker in CUT_MARKERS:
            idx = body.find(marker)
            if idx >= 0:
                body = body[:idx].strip()
        body = re.sub(r"\n목록\s*$", "", body).strip()

        # ── 앞쪽 연속 빈줄 재정리 ────────────────────────────────────────────
        body = body.lstrip('\n').strip()

        youtube_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

        # ── 대표 이미지: og:image → 본문 내 img ──────────────────────────────
        _IMG_BASE = "https://news.mbccb.co.kr"
        def _abs_img(src: str) -> str:
            if not src:
                return ""
            if src.startswith("http"):
                return src
            if src.startswith("/"):
                return _IMG_BASE + src
            return _IMG_BASE + "/" + src

        # 유튜브 영상이 있으면 썸네일을 1순위 이미지로 사용 (실제 존재하는 최고화질 선택)
        if video_id:
            image_url = await best_yt_thumbnail(video_id)
        else:
            image_url = ""

        # 유튜브 썸네일이 없으면 og:image 시도
        if not image_url:
            og_raw = _abs_img(
                (soup.find("meta", attrs={"property": "og:image"}) or {}).get("content", "")
                or (soup.find("meta", attrs={"name": "og:image"}) or {}).get("content", "")
                or (soup.find("meta", attrs={"name": "twitter:image"}) or {}).get("content", "")
            )
            # 아이콘/GIF 등 무의미한 이미지는 무시
            if og_raw and _SKIP_IMG_RE.search(og_raw):
                og_raw = ""
            image_url = og_raw

        # og:image 실패 시 → MBC충북 전용 본문 이미지 영역 우선 탐색
        if not image_url:
            news_img_el = soup.select_one(".news_img")
            if news_img_el:
                for img in news_img_el.find_all("img", src=True):
                    s = _abs_img(img["src"])
                    if s and not _SKIP_IMG_RE.search(s):
                        image_url = s
                        break

        # 그래도 없으면 content_el에서 탐색 (단, 관련뉴스·댓글 영역 제외)
        if not image_url and content_el:
            # 관련 뉴스 목록, 댓글, SNS 공유 등 노이즈 제거
            for noise in content_el.select(
                ".comment_list, [class*=related], [class*=news_list], "
                ".layer_share, [class*=share], [class*=sns], iframe, script, style"
            ):
                noise.decompose()
            for img in content_el.find_all("img", src=True):
                src = img["src"]
                if not src or _SKIP_IMG_RE.search(src):
                    continue
                image_url = _abs_img(src)
                break

        return {
            "source": "mbccb",
            "title": title,
            "url": article_url,
            "body": body,
            "reporter": reporter,
            "published_at": date,
            "image_url": image_url,
            "video_id": video_id,
            "youtube_url": youtube_url,
        }
    except Exception as e:
        logger.warning(f"[MBCCB] 기사 본문 수집 실패 ({article_url}): {e}")
        return {"source": "mbccb", "title": "", "url": article_url, "body": "", "reporter": "", "published_at": ""}


async def recheck_news_articles(urls: list[str], on_save, on_progress=None) -> int:
    """이미 저장된 기사를 다시 열어 영상·대표 이미지를 바로잡는다.
    MBC충북은 기사 발행 몇 시간~며칠 뒤에 유튜브 영상을 임베드하는 경우가 있어, 최초 수집 시점에는 없던 영상이
    기존 기사에 반영되지 않는 문제를 막는다. 영상이 있으면 유튜브 썸네일이, 없으면 그 기사 자신의 og:image 가 이미지가 된다.
    제목·본문 등은 비워 보내 기존 값을 유지(upsert 의 COALESCE)하고 video_id/youtube_url/image_url 만 갱신한다.
    반환: 갱신 요청한 기사 수"""
    import asyncio as _aio
    changed = 0
    total = len(urls)
    for i, u in enumerate(urls):
        if on_progress:
            on_progress(i, total, f"기사 영상·이미지 재확인 중... ({i}/{total})")
        art = await crawl_news_article(None, u)
        if not art.get("title"):      # 수집 실패(네트워크 등)는 건드리지 않는다
            continue
        if art.get("video_id") or art.get("image_url"):
            await on_save({
                "source": "mbccb", "url": u,
                "video_id": art.get("video_id", ""), "youtube_url": art.get("youtube_url", ""),
                "image_url": art.get("image_url", ""),
            })
            changed += 1
        await _aio.sleep(0.2)
    return changed


async def crawl_mbccb_news(context: BrowserContext, max_count: int = 0, on_progress=None,
                          existing_urls: set = None,
                          on_save=None) -> list[dict]:
    """
    MBC충북 뉴스 기사 전체 크롤링 (페이지별 즉시 저장 지원):
    1. 목록 페이지에서 기사 링크 수집 (페이지 단위)
    2. 각 기사 본문 즉시 수집 → on_save 콜백으로 즉시 저장
    반환: [{source, title, url, body, reporter, published_at}]
    on_progress(current, total, message): 진행상황 콜백 (선택)
    existing_urls: 이미 DB에 있는 URL 집합 (중복 방지, 가변 set – 수집 중 갱신됨)
    on_save(article) -> bool: 기사를 즉시 저장하는 콜백 (선택, True=신규 저장)
    """
    import httpx as _httpx
    from bs4 import BeautifulSoup as _BS

    _HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    _SKIP_RE = re.compile(r"icon|ico|logo|comment|banner|btn|arrow|bg_|sprite|common|popup|sc52_video|live_con|\.gif($|\?)", re.I)
    _BASE = "https://news.mbccb.co.kr"
    unlimited = (max_count == 0)

    if existing_urls is None:
        existing_urls = set()

    results = []
    total_collected = 0
    saved_count = 0
    pg = 1

    async with _httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=15) as list_client:
        while True:
            if not unlimited and total_collected >= max_count:
                break

            # ── 목록 페이지 수집 ──────────────────────────────
            list_url = f"{MBCCB_NEWS_LIST_URL}&mod=list&page={pg}"
            try:
                r = await list_client.get(list_url)
                soup = _BS(r.text, "html.parser")
            except Exception as e:
                logger.error(f"[MBCCB] 뉴스 목록 page={pg} 수집 실패: {e}")
                break

            page_items = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "menukey=61" not in href or "mod=view" not in href or "RECEIVE_DATE" not in href:
                    continue
                # 우측 '많이 본 뉴스' 순위 위젯(<span>순위</span>제목)은 제외 — 제목에 순위 숫자가 붙고 썸네일이 없음
                if a.find_parent(class_="cont_right") is not None:
                    continue
                full_url = (_BASE + href if href.startswith("/") else
                            href if href.startswith("http") else _BASE + "/home/" + href)
                # URL 정규화: page=, scode= 등 목록 파라미터 제거 (중복 방지)
                norm_url = full_url
                m_date = re.search(r'RECEIVE_DATE=(\d+)', full_url)
                m_seq  = re.search(r'SEQUENCE=(\d+)', full_url)
                if m_date and m_seq:
                    norm_url = (f"{_BASE}/home/sub.php?menukey=61&mod=view"
                                f"&RECEIVE_DATE={m_date.group(1)}&SEQUENCE={m_seq.group(1)}")
                title = a.get_text(strip=True).replace("\u00a0", " ")
                title = re.sub(r"\s+", " ", title).strip()[:200]
                if not title or len(title) < 3:
                    continue

                # 목록 썸네일 (기존 기사의 이미지 갱신에도 사용)
                thumb = ""
                parent = a.parent
                for _ in range(4):
                    if parent is None:
                        break
                    for img in parent.find_all("img", src=True):
                        if not _SKIP_RE.search(img["src"]):
                            thumb = img["src"]
                            if thumb.startswith("/"):
                                thumb = _BASE + thumb
                            break
                    if thumb:
                        break
                    parent = parent.parent

                # 이미 DB에 있는 기사: 목록 썸네일로 이미지만 갱신 후 스킵
                if norm_url in existing_urls:
                    if thumb and not _SKIP_RE.search(thumb) and on_save:
                        # 썸네일 파일명이 이 기사 날짜/SEQUENCE와 일치할 때만 갱신
                        m_d = re.search(r'RECEIVE_DATE=(\d{8})', norm_url)
                        m_s = re.search(r'SEQUENCE=(\d+)', norm_url)
                        m_t = re.search(r'_(\d{8})_(\d+)\.', thumb)
                        should_update = True
                        if m_d and m_s and m_t:
                            should_update = (m_t.group(1) == m_d.group(1) and
                                             m_t.group(2).lstrip('0') == m_s.group(1).lstrip('0'))
                        if should_update:
                            await on_save({
                                "source": "mbccb", "url": norm_url,
                                # summary는 넘기지 않음 (기존 본문 요약이 제목으로 덮어써지는 것 방지)
                                "title": title,
                                "image_url": thumb,
                            })
                    continue

                # 목록에는 한 기사가 썸네일 링크·제목 링크로 두 번 나온다 → 같은 기사는 하나로 합친다
                # (그러지 않으면 새 기사를 두 번씩 받아 오고, 건수도 두 배로 잡힌다)
                _dup = next((it for it in page_items if it["url"] == norm_url), None)
                if _dup is not None:
                    if not _dup["thumb"] and thumb:
                        _dup["thumb"] = thumb
                    continue
                page_items.append({"url": norm_url, "title": title, "thumb": thumb})

            if not page_items:
                logger.info(f"[MBCCB] 뉴스 목록 page={pg} 기사 없음 → 종료")
                break

            logger.info(f"[MBCCB] 뉴스 목록 page={pg}: {len(page_items)}개 신규")

            # ── 기사 본문 즉시 수집 + 저장 ───────────────────
            for item in page_items:
                if not unlimited and total_collected >= max_count:
                    break
                art = await crawl_news_article(context, item["url"])
                if not art.get("title"):
                    art["title"] = item.get("title", "")
                if not art.get("image_url") and item.get("thumb"):
                    thumb = item["thumb"]
                    # sc52_video.gif 등 무의미한 썸네일 제거
                    if not _SKIP_IMG_RE.search(thumb):
                        # 썸네일 파일명이 기사 날짜/SEQUENCE와 일치하는지 검증
                        # (목록 페이지에서 인접 기사 이미지가 잘못 매핑되는 것 방지)
                        import re as _re_thumb
                        m_d = _re_thumb.search(r'RECEIVE_DATE=(\d{8})', item["url"])
                        m_s = _re_thumb.search(r'SEQUENCE=(\d+)', item["url"])
                        m_t = _re_thumb.search(r'_(\d{8})_(\d+)\.', thumb)
                        if m_d and m_s and m_t:
                            # 날짜 및 SEQUENCE가 일치하는 경우에만 사용
                            if (m_t.group(1) == m_d.group(1) and
                                    m_t.group(2).lstrip('0') == m_s.group(1).lstrip('0')):
                                art["image_url"] = thumb
                        else:
                            # 파일명 패턴이 없으면 그냥 사용 (외부 이미지 등)
                            art["image_url"] = thumb

                existing_urls.add(item["url"])   # 이미 정규화된 URL로 중복 방지
                results.append(art)
                total_collected += 1

                # 즉시 저장 콜백
                if on_save:
                    try:
                        is_new = await on_save(art)
                        if is_new:
                            saved_count += 1
                    except Exception as e:
                        logger.warning(f"[MBCCB] 기사 즉시 저장 실패: {e}")

                logger.info(f"[MBCCB] 기사 수집: {art['title'][:40]}")
                if on_progress:
                    # 개수 제한이 있으면 그 값이 총량, 전체 수집이면 총량을 알 수 없으므로 -1(미정)
                    on_progress(total_collected, -1 if unlimited else max_count, art["title"][:40])
                await asyncio.sleep(0.3)

            pg += 1
            await asyncio.sleep(0.2)

    logger.info(f"[MBCCB] 뉴스 기사 {total_collected}개 수집 완료 ({saved_count}개 신규 저장)")
    if on_progress:
        on_progress(total_collected, total_collected, "완료")
    return results


# ── 다시보기 (VOD) 수집 ──────────────────────────────────────────────────────

def _vod_no(url: str) -> str:
    """다시보기 URL 에서 게시물 번호(no) 추출. page=/scode= 같은 목록 파라미터가 달라도 같은 글을 같게 본다."""
    m = re.search(r"[?&]no=(\d+)", url or "")
    return m.group(1) if m else ""


async def collect_vod_list(context: BrowserContext, max_count: int = 300,
                           existing_nos: set = None) -> list[dict]:
    """MBC충북 다시보기 목록에서 VOD 링크를 수집합니다 (httpx 기반, Playwright 불필요).

    사이트 목록은 '선택한 연/월' 의 글만 보여주므로(월당 약 40~75개) 현재 월부터 과거 방향으로
    월을 바꿔가며 읽는다. existing_nos(이미 수집한 게시물 번호)는 제외하고 '신규' 가 max_count 개가
    될 때까지 계속한다. 가장 오래된 월은 사이트의 연도 선택지(2007~) 기준."""
    import httpx as _httpx
    from datetime import datetime as _dt
    from bs4 import BeautifulSoup as _BS

    _HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    existing_nos = existing_nos or set()
    vods: list[dict] = []
    seen_nos: set[str] = set()
    skipped = 0
    base = "https://news.mbccb.co.kr/home/"

    def _parse(html: str) -> list[dict]:
        out = []
        for a in _BS(html, "html.parser").find_all("a", href=True):
            href = a["href"]
            if "menukey=69" in href and "mod=view" in href:
                no = _vod_no(href)
                title = a.get_text(strip=True)
                if no and title and len(title) > 3:
                    out.append({"no": no, "title": title,
                                "url": f"{base}sub.php?menukey=69&mod=view&no={no}"})
        return out

    try:
        async with _httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=15) as client:
            now = _dt.now()
            # 사이트에서 선택 가능한 가장 이른 연도
            first_year = 2007
            try:
                r0 = await client.get(f"{MBCCB_VOD_LIST_URL}&mod=list&page=1")
                years = [int(o.get("value")) for o in _BS(r0.text, "html.parser").select('select[name="year"] option')
                         if (o.get("value") or "").isdigit()]
                if years:
                    first_year = min(years)
            except Exception:
                pass

            y, m = now.year, now.month
            while (y, m) >= (first_year, 1) and len(vods) < max_count:
                month_new = 0
                for pg in range(1, 60):
                    url = f"{MBCCB_VOD_LIST_URL}&mod=list&cate=&year_now={y}&month_now={m:02d}&page={pg}"
                    r = await client.get(url)
                    items = [it for it in _parse(r.text) if it["no"] not in seen_nos]
                    if not items:
                        break
                    for it in items:
                        seen_nos.add(it["no"])
                        if it["no"] in existing_nos:
                            skipped += 1
                            continue
                        vods.append({"url": it["url"], "title": it["title"]})
                        month_new += 1
                    await asyncio.sleep(0.1)
                    if len(vods) >= max_count:
                        break
                logger.info(f"[MBCCB] 다시보기 {y}-{m:02d}: 신규 {month_new}개 (누적 {len(vods)}개, 기존 건너뜀 {skipped}개)")
                m -= 1
                if m == 0:
                    y, m = y - 1, 12

        logger.info(f"[MBCCB] 다시보기 목록 신규 {len(vods)}개 수집 (최대 {max_count}개 요청, 기존 {skipped}개 제외)")
    except Exception as e:
        logger.error(f"[MBCCB] 다시보기 목록 수집 실패: {e}")
    return vods[:max_count]


async def crawl_vod_detail(context: BrowserContext, vod_url: str, vod_title: str) -> Optional[dict]:
    """다시보기 상세 페이지에서 YouTube iframe ID를 추출합니다 (httpx 기반)."""
    import httpx as _httpx
    from bs4 import BeautifulSoup as _BS

    _HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        async with _httpx.AsyncClient(headers=_HEADERS, follow_redirects=True, timeout=12) as client:
            r = await client.get(vod_url)
        soup = _BS(r.text, "html.parser")
        for iframe in soup.find_all("iframe", src=True):
            vid = _extract_youtube_id(iframe["src"])
            if vid:
                return {"video_id": vid, "page_url": vod_url, "title": vod_title}
        # a[href] 링크에서도 YouTube ID 탐색
        for a in soup.find_all("a", href=True):
            vid = _extract_youtube_id(a["href"])
            if vid:
                return {"video_id": vid, "page_url": vod_url, "title": vod_title}
        return None
    except Exception as e:
        logger.warning(f"[MBCCB] VOD 상세 수집 실패 ({vod_url}): {e}")
        return None


async def crawl_mbccb_vod(context: BrowserContext, max_count: int = 300, on_progress=None,
                         existing_video_ids: set = None, existing_page_urls: set = None) -> list[dict]:
    """
    MBC충북 다시보기 전체 크롤링:
    1. 목록 페이지에서 VOD 링크 수집 (max_count 개)
    2. 각 VOD 상세 페이지에서 YouTube ID 추출 (기존 video_id 건너뜀)
    3. YouTube 제목/설명 수집
    반환: [{source, category, video_id, title, description, youtube_url, page_url, drama_title}]
    on_progress(current, total, message): 진행상황 콜백 (선택)
    existing_video_ids: 이미 DB에 있는 video_id 집합 (중복 방지)
    """
    existing_nos = {_vod_no(u) for u in (existing_page_urls or set()) if _vod_no(u)}
    vod_list = await collect_vod_list(context, max_count, existing_nos)
    total = len(vod_list)
    if on_progress: on_progress(0, total, f"목록 {total}개 수집 완료")

    results = []
    for i, vod in enumerate(vod_list):
        # 이미 수집한 다시보기 페이지는 상세 페이지 접속 자체를 생략 (목록 URL = 저장된 page_url)
        if existing_nos and _vod_no(vod["url"]) in existing_nos:
            if on_progress: on_progress(i + 1, total, f"{vod['title'][:30]} (기존 건너뜀)")
            continue
        detail = await crawl_vod_detail(context, vod["url"], vod["title"])
        if not detail:
            if on_progress: on_progress(i + 1, total, f"{vod['title'][:30]} (YouTube 없음)")
            continue
        vid_id = detail["video_id"]
        # 이미 DB에 있는 video_id 건너뜀
        if existing_video_ids and vid_id in existing_video_ids:
            logger.info(f"[MBCCB VOD] '{vod['title'][:30]}' 이미 저장됨 (video_id={vid_id}), 건너뜀")
            if on_progress: on_progress(i + 1, total, f"{vod['title'][:30]} (기존 건너뜀)")
            continue
        yt_info = await get_youtube_info(context, vid_id)
        entry = {
            "source": "mbccb",
            "category": "vod",
            "video_id": vid_id,
            "title": yt_info.get("title") or vod["title"],
            "description": yt_info.get("description", ""),
            "youtube_url": yt_info.get("youtube_url", f"https://www.youtube.com/watch?v={vid_id}"),
            "page_url": vod["url"],
            "drama_title": vod["title"],
        }
        results.append(entry)
        logger.info(f"[MBCCB] VOD '{vod['title'][:30]}' → video_id={vid_id} 수집 완료")
        if on_progress: on_progress(i + 1, total, vod["title"][:30])
        await asyncio.sleep(1)

    logger.info(f"[MBCCB] 다시보기 {len(results)}개 수집 완료")
    if on_progress: on_progress(total, total, "완료")
    return results


# ── 링크 한 건 수집 (수동검색창에 MBC충북 링크를 넣었을 때) ──────────────────────

_YT_VIDEO_URL_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|live/|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})", re.I)


def classify_mbccb_link(url: str) -> Optional[str]:
    """MBC충북 관련 링크가 어느 탭 항목인지 판별.
    'news' (뉴스 기사) | 'vod' (뉴스 다시보기) | 'live' (뉴스LIVE = 유튜브 영상) | None (해당 없음)"""
    from urllib.parse import urlparse
    u = (url or "").strip()
    host = urlparse(u).netloc.lower().replace("www.", "")
    if host == "mbccb.co.kr" or host.endswith(".mbccb.co.kr"):
        if "menukey=61" in u and "mod=view" in u and re.search(r"RECEIVE_DATE=\d+", u) and re.search(r"SEQUENCE=\d+", u):
            return "news"
        if "menukey=69" in u and "mod=view" in u and _vod_no(u):
            return "vod"
        return None
    if _YT_VIDEO_URL_RE.search(u):
        return "live"   # MBC충북 사이트의 '뉴스 LIVE' 메뉴가 유튜브로 연결되므로, 유튜브 영상 링크는 뉴스LIVE 로 본다
    return None


def is_mbccb_host(url: str) -> bool:
    from urllib.parse import urlparse
    host = urlparse((url or "").strip()).netloc.lower().replace("www.", "")
    return host == "mbccb.co.kr" or host.endswith(".mbccb.co.kr")


async def collect_mbccb_link(context: BrowserContext, url: str, kind: str) -> dict:
    """링크 한 건을 수집해 저장용 dict 로 반환. kind: classify_mbccb_link 결과.
    반환: {"kind", "ok", "error"?, "data"} — data 는 upsert_news_article / upsert_youtube_video 에 그대로 넘길 수 있는 형태."""
    import httpx as _httpx
    from bs4 import BeautifulSoup as _BS
    from core.database import _normalize_news_url

    url = url.strip()
    if kind == "news":
        norm = _normalize_news_url(url)
        art = await crawl_news_article(context, norm)
        if not art.get("title"):
            return {"kind": kind, "ok": False, "error": "뉴스 기사 내용을 읽지 못했습니다. 링크를 확인해 주세요."}
        art["url"] = norm
        return {"kind": kind, "ok": True, "data": art}

    if kind == "vod":
        no = _vod_no(url)
        page_url = f"https://news.mbccb.co.kr/home/sub.php?menukey=69&mod=view&no={no}"
        try:
            async with _httpx.AsyncClient(headers=_NEWS_HTTP_HEADERS, follow_redirects=True, timeout=15) as client:
                r = await client.get(page_url)
            soup = _BS(r.text, "html.parser")
        except Exception as e:
            return {"kind": kind, "ok": False, "error": f"다시보기 페이지에 접속하지 못했습니다: {e}"}
        og = soup.find("meta", attrs={"property": "og:title"})
        page_title = (og.get("content", "").strip() if og else "") or ""
        vid = ""
        for iframe in soup.find_all("iframe", src=True):
            vid = _extract_youtube_id(iframe["src"]) or ""
            if vid:
                break
        if not vid:
            for a in soup.find_all("a", href=True):
                vid = _extract_youtube_id(a["href"]) or ""
                if vid:
                    break
        if not vid:
            return {"kind": kind, "ok": False, "error": "다시보기 페이지에서 유튜브 영상을 찾지 못했습니다."}
        yt = await get_youtube_info(context, vid)
        return {"kind": kind, "ok": True, "data": {
            "source": "mbccb", "category": "vod", "video_id": vid,
            "title": yt.get("title") or page_title,
            "description": yt.get("description", ""),
            "youtube_url": yt.get("youtube_url") or f"https://www.youtube.com/watch?v={vid}",
            "page_url": page_url, "drama_title": page_title or yt.get("title", ""),
        }}

    # live — 유튜브 영상
    m = _YT_VIDEO_URL_RE.search(url)
    vid = m.group(1) if m else ""
    if not vid:
        return {"kind": kind, "ok": False, "error": "유튜브 영상 ID를 찾지 못했습니다."}
    yt = await get_youtube_info(context, vid)
    if not yt.get("title"):
        return {"kind": kind, "ok": False, "error": "유튜브 영상 정보를 읽지 못했습니다."}
    return {"kind": kind, "ok": True, "data": {
        "source": "mbccb", "category": "live", "video_id": vid,
        "title": yt.get("title", ""), "description": yt.get("description", ""),
        "youtube_url": f"https://www.youtube.com/watch?v={vid}",
        "page_url": "", "drama_title": "",
    }}
