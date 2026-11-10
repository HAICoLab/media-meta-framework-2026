"""
com2usN 공식 홈페이지 드라마 크롤러
- https://www.com2usn.com/work/category/2 목록에서 드라마 상세 페이지 탐색
- 유튜브 iframe embed URL → 비디오 ID 추출 → YouTube 제목/설명 수집
- 위키피디아 보충 정보는 wiki.resolver 로 위키피디아 보충
"""

import asyncio
import logging
import re
import json
from typing import Optional
from playwright.async_api import BrowserContext

from crawlers.youtube import (
    extract_youtube_id as _extract_youtube_id,
    get_youtube_info,
    best_yt_thumbnail as _best_yt_thumbnail,
)

logger = logging.getLogger(__name__)

COM2USN_WORK_URL = "https://www.com2usn.com/work/category/2"


_WORK_URL_RE = re.compile(r"^https?://(?:www\.)?com2usn\.com/work/(\d+)/?(?:[?#].*)?$", re.IGNORECASE)


def is_com2usn_work_url(url: str) -> bool:
    """com2usN 작품 상세 URL(예: https://www.com2usn.com/work/255) 인가."""
    return bool(_WORK_URL_RE.match((url or "").strip()))


def canon_com2usn_work_url(url: str) -> str:
    m = _WORK_URL_RE.match((url or "").strip())
    return f"https://www.com2usn.com/work/{m.group(1)}" if m else (url or "").strip()


async def fetch_com2usn_work_title(context: BrowserContext, url: str) -> str:
    """작품 상세 페이지의 제목(h1). 페이지 <title>·og 는 사이트 공통 문구('WORK | 컴투스엔')라 쓸 수 없다."""
    page = None
    try:
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        try:
            await page.wait_for_selector("h1", timeout=8000)
        except Exception:
            pass
        title = await page.evaluate("() => (document.querySelector('h1')?.textContent || '').trim()")
        return re.sub(r"\s+", " ", title or "").strip()
    except Exception as e:
        logger.warning(f"[com2usN] 작품 제목 읽기 실패 ({url}): {e}")
        return ""
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def crawl_com2usn_drama_detail(context: BrowserContext, drama_url: str, drama_title: str) -> Optional[dict]:
    """com2usN 드라마 상세 페이지에서 YouTube embed ID 및 대표 이미지를 추출합니다."""
    page = None
    try:
        page = await context.new_page()
        await page.goto(drama_url, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(1500)

        # iframe src 추출
        iframes = await page.evaluate(
            "JSON.stringify(Array.from(document.querySelectorAll('iframe')).map(f=>f.src))"
        )
        iframes = json.loads(iframes)

        video_id = None
        for src in iframes:
            vid = _extract_youtube_id(src)
            if vid:
                video_id = vid
                break

        if not video_id:
            # <a href> 유튜브 링크
            links = await page.evaluate(
                "JSON.stringify(Array.from(document.querySelectorAll('a[href*=\"youtube\"],a[href*=\"youtu.be\"]')).map(a=>a.href))"
            )
            links = json.loads(links)
            for lnk in links:
                vid = _extract_youtube_id(lnk)
                if vid:
                    video_id = vid
                    break

        # ── 대표 이미지 수집 (YouTube 유무와 관계없이) ──────────────────────
        # _next/image 원본 URL 우선 → og:image 폴백 (og:image는 사이트 공통 기본 이미지일 수 있음)
        poster = await page.evaluate("""() => {
            const skipRe = /logo|icon|btn|arrow|sprite|common|favicon|naver|kakao/i;
            // 1순위: Next.js _next/image에서 원본 URL 추출 (드라마별 실제 이미지)
            for (const img of document.querySelectorAll('img[src*="_next/image"]')) {
                const src = img.src || '';
                if (skipRe.test(src)) continue;
                try {
                    const u = new URL(src);
                    const origUrl = u.searchParams.get('url');
                    if (origUrl) return decodeURIComponent(origUrl);
                } catch(e) {}
            }
            // 2순위: 일반 img[src] 중 외부 URL
            for (const img of document.querySelectorAll('img[src]')) {
                const src = img.src || '';
                if (skipRe.test(src) || src.includes('_next/')) continue;
                if (src.startsWith('http')) return src;
            }
            // 3순위: og:image
            return document.querySelector('meta[property="og:image"]')?.content || '';
        }""")

        # 이미지 우선순위: YouTube 썸네일 > api.com2usn.com 이미지 > 기타 외부 URL
        # api.com2usn.com/files/...webp 는 실제로 200 OK로 접근 가능한 이미지 서버
        if video_id:
            yt_poster = await _best_yt_thumbnail(video_id)
            if yt_poster:
                poster = yt_poster
                logger.info(f"[com2usN] {drama_title} → YouTube 썸네일 사용 (video_id={video_id})")
            elif poster and poster.startswith("http"):
                logger.info(f"[com2usN] {drama_title} → YouTube 썸네일 실패, 사이트 이미지 사용: {poster[:60]}")
            # else: poster 빈값 유지
        elif not poster or not poster.startswith("http"):
            poster = ""

        if not video_id:
            logger.info(f"[com2usN] {drama_title} → YouTube 없음, 이미지={poster[:60] if poster else '없음'}")
            return {"drama_title": drama_title, "page_url": drama_url, "video_id": None, "poster": poster}

        return {"drama_title": drama_title, "page_url": drama_url, "video_id": video_id, "poster": poster}
    except Exception as e:
        logger.warning(f"[com2usN] {drama_title} 상세 크롤링 실패: {e}")
        return None
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass


async def collect_com2usn_drama_list(context: BrowserContext) -> list[dict]:
    """com2usN 드라마 목록 페이지에서 드라마 링크 목록을 수집합니다.
    컨텍스트가 닫혀있으면 최대 2회 재시도합니다.
    """
    dramas = []

    for attempt in range(3):
        page = None
        try:
            page = await context.new_page()
            await page.goto(COM2USN_WORK_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            links_json = await page.evaluate("""JSON.stringify(
                Array.from(document.querySelectorAll('a[href*="/work/"]'))
                    .filter(a => /\\/work\\/\\d+/.test(a.href))
                    .map(a => ({
                        href: a.href,
                        text: a.textContent.trim().replace(/\\s+/g, ' ')
                    }))
            )""")
            links = json.loads(links_json)

            seen = set()
            for lnk in links:
                href = lnk["href"]
                if href in seen:
                    continue
                seen.add(href)
                # 텍스트에서 'Content 드라마' 부분 제거
                title = re.sub(r"\s*Content\s*(드라마|예능|영화)?\s*$", "", lnk["text"]).strip()
                if title:
                    dramas.append({"title": title, "url": href})

            logger.info(f"[com2usN] 드라마 {len(dramas)}개 목록 수집 완료")
            break  # 성공 시 루프 탈출

        except Exception as e:
            _closed = any(kw in str(e).lower() for kw in ("closed", "target", "has been"))
            if _closed and attempt < 2:
                logger.warning(f"[com2usN] 목록 수집 실패 (컨텍스트 닫힘, 재시도 {attempt+1}/2): {e}")
                await asyncio.sleep(2)
                # 닫힌 컨텍스트로는 몇 번을 다시 해도 실패하므로, 살아 있는(필요하면 재시작된) 컨텍스트로 교체한다
                try:
                    from core.browser import get_context as _get_live_context
                    context = await _get_live_context()
                except Exception as _ce:
                    logger.warning(f"[com2usN] 브라우저 컨텍스트 복구 실패: {_ce}")
                continue
            logger.error(f"[com2usN] 목록 수집 실패: {e}")
            break
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

    return dramas


async def find_com2usn_drama_by_title(context: BrowserContext, search_title: str) -> Optional[dict]:
    """com2usN 드라마 목록에서 제목으로 드라마를 검색하고 YouTube 정보를 반환합니다.
    
    Returns:
        {"video_id", "youtube_url", "title", "description", "drama_title", "page_url"}
        또는 None (찾지 못한 경우)
    """
    dramas = await collect_com2usn_drama_list(context)
    if not dramas:
        return None

    # 제목 정규화 (공백·특수문자 제거 후 비교)
    def _norm(t: str) -> str:
        return re.sub(r"[\s\W]", "", t).lower()

    norm_search = _norm(search_title)
    best = None
    best_score = 0
    for d in dramas:
        norm_d = _norm(d["title"])
        # 완전 일치 → 즉시 선택
        if norm_search == norm_d:
            best = d
            break
        # 부분 포함: 짧은 쪽의 길이가 긴 쪽의 70% 이상일 때만 허용 (느슨한 부분 매칭 방지)
        if norm_search in norm_d or norm_d in norm_search:
            short_len = min(len(norm_search), len(norm_d))
            long_len  = max(len(norm_search), len(norm_d))
            ratio = short_len / long_len if long_len else 0
            if ratio >= 0.7 and ratio > best_score:
                best_score = ratio
                best = d

    if not best:
        logger.info(f"[com2usN] '{search_title}' → 목록에서 찾지 못함")
        return None

    logger.info(f"[com2usN] '{search_title}' → '{best['title']}' 매칭, URL={best['url']}")
    detail = await crawl_com2usn_drama_detail(context, best["url"], best["title"])
    if not detail or not detail.get("video_id"):
        logger.info(f"[com2usN] '{best['title']}' → YouTube 영상 없음")
        return None

    yt_info = await get_youtube_info(context, detail["video_id"])
    return {
        **yt_info,
        "drama_title": best["title"],
        "page_url": best["url"],
        "source": "com2usn",
        "category": "drama",
    }


_SKIP_SECTIONS = {"각주", "같이 보기", "외부 링크", "동시간대", "참고 사항", "외부링크",
                   "참고문헌", "각주 및 참고 문헌", "주석", "더 보기"}

def _parse_wiki_sections(sections: list) -> list:
    """sections의 texts/tables/subsections를 재귀적으로 정리합니다.
    메타 섹션(각주·같이 보기·외부 링크 등)은 제거합니다.
    """
    result = []
    for sec in sections:
        title = sec.get("title", "")
        if title in _SKIP_SECTIONS:
            continue
        s = {"title": title, "texts": sec.get("texts", [])}
        # 테이블이 있으면 포함
        if sec.get("tables"):
            s["tables"] = sec["tables"]
        # 하위 섹션 재귀
        if sec.get("subsections"):
            s["subsections"] = _parse_wiki_sections(sec["subsections"])
        result.append(s)
    return result


def _format_wiki_supplement(wiki: dict) -> dict:
    """parse_wiki_page / parse_wiki_from_dump 결과 → 편의 필드 포함 보충 dict."""
    infobox = wiki.get("infobox", {})

    def _split(val: str) -> list:
        val = re.sub(r"\s*\([^)]*\)", "", val or "")
        parts = re.split(r"[,，、\n]+", val)
        return [p.strip() for p in parts if p.strip()]

    return {
        "wiki_url":       wiki.get("wiki_url", ""),
        "wiki_poster":    wiki.get("wiki_poster", ""),
        "summary":        wiki.get("summary", ""),
        "infobox":        infobox,
        "genre":          infobox.get("장르", ""),
        "channel":        infobox.get("방송 채널") or infobox.get("채널") or "",
        "air_date":       infobox.get("방송 기간") or infobox.get("방영 기간") or "",
        "episodes":       infobox.get("방송 횟수") or infobox.get("편수") or "",
        "director":       _split(infobox.get("연출", "") or infobox.get("감독", "")),
        "writer":         _split(infobox.get("극본", "") or infobox.get("각본", "")),
        "cast":           _split(infobox.get("출연자", "") or infobox.get("출연", "")),
        "production":     infobox.get("제작사", ""),
        "air_time":       infobox.get("방송 시간", ""),
        "sections":       _parse_wiki_sections(wiki.get("sections", [])),
        "external_links": wiki.get("external_links", []),
    }


_WIKI_HEADERS = {
    # Wikipedia 봇 정책 준수 User-Agent (이메일 포함 필수)
    "User-Agent": "KDramaResearch/1.0 (Academic Research; admin@example.com)",
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
_WIKI_API = "https://ko.wikipedia.org/w/api.php"


async def _fetch_wiki_supplement(context: BrowserContext, drama_title: str) -> dict:
    """drama_title로 위키피디아 보충 정보를 가져옵니다.

    문서 선택/본문 수집은 수동검색·CJ ENM 과 공용인 wiki_resolver 를 사용합니다.
    (Dumps 기준으로 드라마/예능 문서만 채택, 없으면 '위키피디아 문서가 없습니다')
    """
    from wiki.resolver import resolve_wiki_page, NO_WIKI_TEXT

    try:
        # com2usN 은 한국 드라마만 다룬다 → 같은 제목의 해외 작품(넘버스: 미국 CBS)·다큐 문서는 거른다
        wiki = await resolve_wiki_page(drama_title, content_type="drama", hint={"origin": "domestic"})
    except Exception as e:
        logger.warning(f"[com2usN] 위키피디아 보충 실패 ({drama_title}): {e}")
        return {}

    if not wiki:  # 검색/네트워크 실패 → 문서 없음으로 단정하지 않음
        return {}
    if wiki.get("no_wiki"):
        logger.info(f"[com2usN] '{drama_title}' → 위키피디아 문서 없음")
        return {"no_wiki": True, "summary": NO_WIKI_TEXT}

    logger.info(f"[com2usN] '{drama_title}' 위키 보충 성공 (sections={len(wiki.get('sections') or [])})")
    return _format_wiki_supplement(wiki)


async def _get_fresh_context(context: BrowserContext) -> BrowserContext:
    """브라우저 컨텍스트가 닫혀있으면 재시작한 컨텍스트를 반환합니다."""
    try:
        # 새 페이지를 만들어 컨텍스트 활성 여부 확인
        _p = await context.new_page()
        await _p.close()
        return context
    except Exception:
        logger.warning("[com2usN] 브라우저 컨텍스트 닫힘 감지 → 재연결 시도")
        try:
            from core.browser import get_context as _gc
            fresh = await _gc()
            if fresh:
                logger.info("[com2usN] 브라우저 재연결 성공")
                return fresh
        except Exception as e2:
            logger.error(f"[com2usN] 브라우저 재연결 실패: {e2}")
        return context


async def crawl_com2usn_all(context: BrowserContext, max_count: int = 300, on_progress=None,
                           existing_video_ids: set = None, existing_page_urls: set = None) -> list[dict]:
    """
    com2usN 전체 드라마 크롤링:
    1. 드라마 목록 수집 (com2usN 공식 사이트, 최대 max_count개 / 0 = 전체)
    2. 각 상세 페이지에서 YouTube ID 추출 (기존 video_id 건너뜀)
    3. YouTube 제목/설명 수집
    4. 위키피디아에서 시놉시스·출연진·방영정보 보충
    반환: [{source, category, video_id, title, description, youtube_url, page_url, drama_title, wiki_data}]
    on_progress(current, total, message): 진행상황 콜백 (선택)
    existing_video_ids: 이미 DB에 있는 video_id 집합 (중복 방지)
    """
    drama_list = await collect_com2usn_drama_list(context)
    if max_count and max_count > 0:   # 0 이하 = 전체
        drama_list = drama_list[:max_count]
    total = len(drama_list)
    logger.info(f"[com2usN] 수집 대상: {total}개 (요청 max_count={max_count or '전체'})")
    if on_progress: on_progress(0, total, f"목록 {total}개 수집 완료")

    results = []
    consecutive_errors = 0  # 연속 오류 카운터 (일정 수 이상이면 브라우저 문제로 판단)

    for i, drama in enumerate(drama_list):
        # wiki_ prefix video_id는 드라마 제목 기반으로 사전 계산
        wiki_vid_id = f"wiki_{re.sub(r'[^a-zA-Z0-9가-힣]', '_', drama['title'])}"

        # YouTube 까지 수집 완료된 작품은 상세 페이지 접속 자체를 생략
        # (existing_page_urls 에는 wiki_ 대체 항목이 빠져 있어, YouTube 가 없던 작품은 계속 재시도된다)
        if existing_page_urls and drama["url"] in existing_page_urls:
            if on_progress: on_progress(i + 1, total, f"{drama['title']} (기존 건너뜀)")
            continue

        # 브라우저 컨텍스트 유효성 확인 및 재연결
        context = await _get_fresh_context(context)

        try:
            detail = await crawl_com2usn_drama_detail(context, drama["url"], drama["title"])
            consecutive_errors = 0  # 성공하면 초기화
        except Exception as _e:
            logger.warning(f"[com2usN] '{drama['title']}' 상세 크롤링 예외: {_e}")
            consecutive_errors += 1
            if consecutive_errors >= 5:
                logger.error("[com2usN] 연속 5회 오류 발생 → 크롤링 중단")
                break
            if on_progress: on_progress(i + 1, total, f"{drama['title']} (오류 건너뜀)")
            await asyncio.sleep(2)
            continue
        if not detail or not detail.get("video_id"):
            # YouTube를 찾지 못한 경우
            # wiki_ prefix로 이미 DB에 있어도 → 위키데이터 재시도 (User-Agent 수정 후)
            poster = (detail or {}).get("poster", "")
            page_url = (detail or {}).get("page_url", drama["url"])
            logger.info(f"[com2usN] '{drama['title']}' → YouTube 없음, 위키 보충 시도")
            wiki_data = await _fetch_wiki_supplement(context, drama["title"])
            entry = {
                "source":      "com2usn",
                "category":    "drama",
                "video_id":    wiki_vid_id,
                "title":       drama["title"],
                "description": wiki_data.get("summary", "") if wiki_data else "",
                "youtube_url": "",
                "page_url":    page_url,
                "drama_title": drama["title"],
                "poster":      poster,
                "wiki_data":   wiki_data or {},
            }
            results.append(entry)
            if on_progress: on_progress(i + 1, total, drama["title"])
            await asyncio.sleep(0.5)
            continue

        # 이미 DB에 있는 video_id는 건너뜀 (wiki_ 항목은 video_id가 달라서 재수집됨)
        if existing_video_ids and detail["video_id"] in existing_video_ids:
            logger.info(f"[com2usN] '{drama['title']}' 이미 저장됨 (video_id={detail['video_id']}), 건너뜀")
            if on_progress: on_progress(i + 1, total, f"{drama['title']} (기존 건너뜀)")
            continue

        # YouTube 정보 수집 (실패해도 계속 진행)
        yt_info = {}
        try:
            context = await _get_fresh_context(context)
            yt_info = await get_youtube_info(context, detail["video_id"])
        except Exception as _yt_e:
            logger.warning(f"[com2usN] '{drama['title']}' YouTube 정보 수집 실패 (계속): {_yt_e}")

        # 위키피디아 보충
        wiki_data = await _fetch_wiki_supplement(context, drama["title"])

        _yt_url = yt_info.get("youtube_url") or f"https://www.youtube.com/watch?v={detail['video_id']}"
        entry = {
            "source":      "com2usn",
            "category":    "drama",
            "video_id":    detail["video_id"],
            "title":       yt_info.get("title") or drama["title"],
            "description": yt_info.get("description", ""),
            "youtube_url": _yt_url,
            "page_url":    detail["page_url"],
            "drama_title": drama["title"],
            "poster":      detail.get("poster", ""),  # YouTube thumbnail (crawl_com2usn_drama_detail에서 설정)
            "wiki_data":   wiki_data,
        }
        results.append(entry)
        logger.info(f"[com2usN] '{drama['title']}' → video_id={detail['video_id']}, wiki={'있음' if wiki_data else '없음'}")
        if on_progress: on_progress(i + 1, total, drama["title"])
        await asyncio.sleep(0.5)

    logger.info(f"[com2usN] 전체 {len(results)}개 수집 완료")
    if on_progress: on_progress(total, total, "완료")
    return results
