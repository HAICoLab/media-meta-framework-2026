"""위키 인포박스 → 라이브러리 metadata_json 조립, 단건 수집 파이프라인."""

import asyncio
import logging
import re
import json
from typing import Optional
from playwright.async_api import async_playwright, BrowserContext
from bs4 import BeautifulSoup

from wiki.parse import parse_wiki_page, _WIKI_HEADERS, _clean_infobox_val
from crawlers.official import scrape_official_site
from crawlers.filters import (
    rank_official_sites,
    _is_archive_url,
    _is_homepage_url,
    _is_known_drama_domain,
)

# ── 위키피디아 방송사(채널) 필터 설정 ──────────────────────────────────────
# 위키피디아 목록 표의 "방송사(채널)" 셀에 표시되는 실제 채널명
WIKI_BROADCASTER_CHANNELS: dict[str, set[str]] = {
    "CJ": {"tvN", "OCN", "Mnet", "tvN STORY", "tvN Drama", "OCN Movies"},
    "Com2usN": {"Com2usN", "com2usN", "Com2us N", "com2us N"},
}

def _parse_broadcast_period(raw: str) -> dict:
    """'2024년 1월 2일 ~ 2024년 2월 27일' → {start, end} ISO 날짜"""
    import re as _re
    date_pat = _re.compile(
        r"(\d{4})[년.\-/]?\s*(\d{1,2})[월.\-/]?\s*(\d{1,2})"
    )
    dates = date_pat.findall(raw or "")
    def to_iso(t): return f"{t[0]}-{int(t[1]):02d}-{int(t[2]):02d}"
    if len(dates) >= 2:
        return {"start": to_iso(dates[0]), "end": to_iso(dates[1])}
    if len(dates) == 1:
        return {"start": to_iso(dates[0]), "end": None}
    return {"start": None, "end": None}


def _split_people(val: str) -> list[str]:
    """이름 문자열을 분리. ·, ,, /, \n 등을 구분자로 사용."""
    if not val:
        return []
    parts = re.split(r"[,·/\n]", val)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) >= 2]


_CHANNEL_KO_MAP = {
    "엠넷": "Mnet",
    "티빙": "TVING",
    "티브이엔": "tvN",
}

def _first_channel(val: str) -> str:
    """'tvN , TVING' → 'tvN' (첫 번째 채널만). 한국어 채널명 → 영문 변환 포함."""
    if not val:
        return ""
    ch = re.split(r"[,\s]+", val.strip())[0].strip()
    return _CHANNEL_KO_MAP.get(ch, ch)


def _build_metadata(title: str, wiki: dict, _site: dict = None) -> dict:
    """위키피디아 인포박스 기반 메타데이터 조립.

    _site 파라미터는 하위 호환성을 위해 유지하지만 사용하지 않는다.
    """
    infobox = wiki.get("infobox", {})

    def _v(*keys: str) -> str:
        """인포박스에서 여러 키를 순서대로 시도, 첫 번째 값 반환 (정리 포함)"""
        for k in keys:
            val = infobox.get(k, "")
            cleaned = _clean_infobox_val(val)
            if cleaned:
                return cleaned
        return ""

    # ── 방영 채널 ────────────────────────────────────────────────────────────
    channel_raw = _v("방송 채널", "방송사")
    channel = _first_channel(channel_raw)                    # 첫 번째 채널
    additional_channels = _v("추가 채널")

    # ── Com2usN 자회사 제작사 기반 채널 오버라이드 ──────────────────────────
    # 위지윅스튜디오·엔피·래몽래인·에이투지엔터테인먼트가 제작사에 포함되면
    # 방영 채널과 관계없이 com2usN 채널로 분류
    _COM2USN_STUDIOS = ["위지윅", "엔피", "래몽래인", "에이투지"]
    _production_text = " ".join([
        _v("제작사"), _v("제작"), _v("제작총괄"), _v("프로덕션"),
    ])
    if any(s in _production_text for s in _COM2USN_STUDIOS):
        channel = "com2usN"

    # ── 방영 기간 ────────────────────────────────────────────────────────────
    period_raw = _v("방송 기간", "방영 기간")
    broadcast_period = _parse_broadcast_period(period_raw)
    broadcast_period["raw"] = period_raw

    # ── 방송 시간 (편성 요일+시간) ────────────────────────────────────────────
    broadcast_time = _v("방송 시간", "방영 시간", "방송 요일")

    # ── 방송 분량 (에피소드 러닝타임) ─────────────────────────────────────────
    episode_duration = _v("방송 분량", "방영 분량")

    # ── 에피소드 수 ──────────────────────────────────────────────────────────
    ep_raw = _v("방송 횟수", "편수", "방영 횟수")
    try:
        episode_count = int(re.search(r"\d+", ep_raw).group()) if ep_raw else None
    except Exception:
        episode_count = None

    # ── 장르 ─────────────────────────────────────────────────────────────────
    genre = _v("장르")

    # ── 방송 국가 ────────────────────────────────────────────────────────────
    broadcast_country = _v("방송 국가")

    # ── 제작 관련 ────────────────────────────────────────────────────────────
    planner = _v("기획")
    executive_producer = _v("책임프로듀서", "책임 프로듀서", "책임프로듀서")
    general_producer   = _v("제작총괄", "제작 총괄")
    producer           = _v("프로듀서")
    production_company = _v("제작사")

    # ── 연출 / 극본 ───────────────────────────────────────────────────────────
    director = _split_people(_v("연출", "감독"))
    writer   = _split_people(_v("극본", "각본", "작가"))

    # ── 출연자 ───────────────────────────────────────────────────────────────
    cast_raw = _v("출연자", "출연")
    # "이민호 , 공효진 외" → 분리 후 " 외", " 등" 접미어 제거
    _cast_parts = _split_people(cast_raw)
    cast = []
    for p in _cast_parts:
        p = re.sub(r"\s*(외|등|주연|조연)$", "", p).strip()
        if p and p not in {"외", "등", "주연", "조연"}:
            cast.append(p)

    # ── 음악 ─────────────────────────────────────────────────────────────────
    music = _v("음악")

    # ── 등급 ─────────────────────────────────────────────────────────────────
    content_rating = _v("방송 등급", "시청 등급")

    return {
        "title":               title,
        "genre":               genre or None,
        "broadcast_country":   broadcast_country or None,
        "channel":             channel or None,
        "additional_channels": additional_channels or None,
        "broadcast_period":    broadcast_period,
        "broadcast_time":      broadcast_time or None,
        "episode_duration":    episode_duration or None,
        "episode_count":       episode_count,
        "planner":             planner or None,
        "executive_producer":  executive_producer or None,
        "general_producer":    general_producer or None,
        "producer":            producer or None,
        "production_company":  production_company or None,
        "director":            director,
        "writer":              writer,
        "cast":                cast,
        "music":               music or None,
        "content_rating":      content_rating or None,
        "description":         wiki.get("summary") or None,
        "poster_url":          wiki.get("wiki_poster") or None,
        "wikipedia_url":       wiki.get("wiki_url") or None,
        # 인포박스·위키 섹션은 metadata 에 넣지 않는다 (wikipedia 데이터가 정본이고 DB 에서도 별도 컬럼에만 저장)
    }


# ── 단일 드라마 전체 파이프라인 ──────────────────────────────────────────────

async def collect_drama_metadata_with_context(
    context: BrowserContext,
    drama_title: str,
    wiki_url: Optional[str] = None,
    preset_official_url: Optional[str] = None,
    wiki_only: bool = False,
    origin: str = "",
) -> dict:
    """이미 열려있는 브라우저 컨텍스트를 재사용해 메타데이터 수집.

    Args:
        preset_official_url: 방송사 직접 크롤링 시 이미 알고 있는 공식 사이트 URL.
                             설정되면 Wikipedia 외부링크 검색 없이 이 URL을 직접 사용.
        wiki_only: True이면 공식 사이트 방문 없이 Wikipedia 데이터만으로 메타데이터 생성.
        origin: "domestic"(국내) / "foreign"(해외) — 같은 제목의 반대편 작품 위키 문서를 거른다. ""이면 제한 없음.
    """
    import httpx as _httpx
    result = {
        "title": drama_title,
        "wikipedia": {},
        "official_site": {},
        "official_site_url": None,
        "error": None,
    }
    try:
        # ── 공식 사이트 URL이 직접 주어진 경우: 사이트 먼저 방문해 제목 확보 ──
        page = await context.new_page()
        _page_opened = True
        official_meta_pre: dict = {}

        if preset_official_url and (not drama_title or drama_title == "URL 직접 수집"):
            # ── 1순위: httpx로 빠르게 og:title / <title> 가져오기 (SSR 사이트 대응) ──
            logging.info(f"[preset] httpx로 제목 먼저 추출 시도: {preset_official_url}")
            _http_title = ""
            try:
                import httpx as _httpx_title
                _hdr = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept-Language": "ko-KR,ko;q=0.9",
                }
                async with _httpx_title.AsyncClient(headers=_hdr, follow_redirects=True, timeout=15) as _hc:
                    _hr = await _hc.get(preset_official_url)
                    _hsoup = BeautifulSoup(_hr.text, "lxml")
                    # og:title 우선, 없으면 <title>
                    _og_title_tag = _hsoup.find("meta", property="og:title")
                    _http_title = (_og_title_tag.get("content", "").strip() if _og_title_tag else "") or \
                                  (_hsoup.find("title").get_text(strip=True) if _hsoup.find("title") else "")
                    # "제목 | 사이트명" 형태에서 사이트명 제거
                    _http_title = re.sub(r"\s*[|\-–]\s*.+$", "", _http_title).strip()
                    logging.info(f"[preset] httpx 제목 추출: {_http_title!r}")
                    # og:image도 함께 수집 (Playwright가 SPA에서 못 가져오는 경우 대비)
                    _og_img_tag = _hsoup.find("meta", property="og:image")
                    _http_og_image = _og_img_tag.get("content", "").strip() if _og_img_tag else ""
                    if _http_og_image:
                        # URL 인코딩된 한글 경로 등 처리
                        from urllib.parse import quote as _qenc
                        _http_og_image = re.sub(
                            r'[가-힣ㄱ-ㅎㅏ-ㅣ]+',
                            lambda m: _qenc(m.group(0), encoding='utf-8'),
                            _http_og_image,
                        )
                        result["_httpx_og_image"] = _http_og_image
                        logging.info(f"[preset] httpx og:image 추출: {_http_og_image!r}")
            except Exception as _he:
                logging.info(f"[preset] httpx 제목 추출 실패: {_he}")

            if _http_title:
                drama_title = _http_title
                result["title"] = drama_title
                logging.info(f"[preset] httpx 제목 사용: {drama_title}")
            else:
                # ── 2순위: Playwright로 방문해 제목 추출 (JS 렌더링 필요한 사이트) ──
                logging.info(f"[preset] Playwright로 공식사이트 방문해 제목 추출: {preset_official_url}")
                official_meta_pre = await scrape_official_site(page, preset_official_url)
                _extracted_title = (
                    official_meta_pre.get("structured", {}).get("name", "")
                    or official_meta_pre.get("og", {}).get("title", "")
                    or official_meta_pre.get("page_title", "")
                )
                if _extracted_title:
                    _extracted_title = re.sub(r"\s*[|\-–]\s*.+$", "", _extracted_title).strip()
                    # 스트리밍 서비스 이름 자체(Netflix, 티빙 등)가 제목으로 추출되는 오인 방지
                    _GENERIC_SITE_NAMES = {
                        "netflix", "tving", "wavve", "watcha", "disney+", "disneyplus",
                        "coupang play", "seezn", "viki", "viu", "naver", "kakao",
                        "youtube", "tvn", "kbs", "mbc", "sbs", "jtbc", "ocn",
                    }
                    if _extracted_title.lower() in _GENERIC_SITE_NAMES:
                        logging.info(f"[preset] Playwright 제목이 서비스명이므로 무시: {_extracted_title!r}")
                    else:
                        drama_title = _extracted_title
                        result["title"] = drama_title
                        logging.info(f"[preset] Playwright 제목 추출 성공: {drama_title}")

        # ── Wikipedia 파싱: httpx 사용 (Playwright 불필요, 브라우저 크래시 방지) ──
        wiki_data: dict = {}
        if drama_title and drama_title not in ("URL 직접 수집", ""):
            async with _httpx.AsyncClient(headers=_WIKI_HEADERS, follow_redirects=True, timeout=25) as wiki_client:
                if wiki_url:
                    # 위키 URL 을 직접 지정한 경우: 그 문서를 그대로 파싱
                    wiki_data = await parse_wiki_page(wiki_client, wiki_url)
                else:
                    # 제목 검색: CJ ENM / com2usN 자동수집과 동일한 공용 resolver 사용
                    from wiki.resolver import resolve_wiki_page
                    wiki_data = await resolve_wiki_page(drama_title, hint={"origin": origin} if origin else None)
                    if not wiki_data:  # 검색 자체 실패(네트워크 등)
                        wiki_data = {"error": "위키피디아 페이지를 불러올 수 없습니다.", "wiki_url": ""}
        result["wikipedia"] = wiki_data

        # ── wiki_only 모드: 공식 사이트 방문 없이 Wikipedia 데이터만 사용 ──
        if wiki_only:
            result["metadata"] = _build_metadata(drama_title, wiki_data, {})
            return result

        # preset_official_url이 있으면 Wikipedia 외부링크 검색 없이 직접 사용
        if preset_official_url:
            logging.info(f"[{drama_title}] preset_official_url 사용: {preset_official_url}")
            # 이미 방문한 경우 official_meta_pre 재사용, 아닌 경우 새로 방문
            official_meta = official_meta_pre if official_meta_pre else await scrape_official_site(page, preset_official_url)
            final_url = official_meta.get("url", preset_official_url)
            # 리다이렉트돼서 홈페이지/아카이브/무관 도메인으로 간 경우 official_site 비움
            if _is_archive_url(final_url) or _is_homepage_url(final_url):
                logging.info(f"[{drama_title}] preset_official_url이 홈/아카이브로 리다이렉트됨 → official_site 제외 ({final_url})")
                result["official_site"] = {}
                result["metadata"] = _build_metadata(drama_title, wiki_data, {})
            else:
                result["official_site_url"] = final_url
                result["official_site"] = official_meta
                result["metadata"] = _build_metadata(drama_title, wiki_data, official_meta)
            return result

        # 후보 목록을 점수순으로 정렬 후 순차 시도
        # (한 후보가 만료 도메인으로 리다이렉트되면 다음 후보를 시도)
        ext_links_for_rank = wiki_data.get("external_links", [])

        # rate limiting 등으로 external_links가 비어있으면 DB에서 보완
        if not ext_links_for_rank and drama_title:
            try:
                import aiosqlite as _aiosqlite_fallback, json as _json_fallback
                from core.paths import DB_PATH as _db_path
                async with _aiosqlite_fallback.connect(str(_db_path)) as _db_fb:
                    _cur_fb = await _db_fb.execute(
                        "SELECT external_links_json FROM dramas WHERE title=? LIMIT 1",
                        (drama_title,)
                    )
                    _row_fb = await _cur_fb.fetchone()
                    if _row_fb and _row_fb[0]:
                        _db_ext = _json_fallback.loads(_row_fb[0])
                        if _db_ext:
                            logging.info(f"[{drama_title}] external_links 없음 → DB에서 {len(_db_ext)}개 보완")
                            ext_links_for_rank = _db_ext
            except Exception as _dbe:
                logging.warning(f"[{drama_title}] DB external_links 보완 실패: {_dbe}")

        candidates = rank_official_sites(ext_links_for_rank)
        found_valid = False
        fallback_url = None  # 원본 URL이 드라마 도메인이지만 리다이렉트 검증 실패한 경우 보관
        for official in candidates:
            orig_url = official["url"]
            official_meta = await scrape_official_site(page, orig_url)
            final_url = official_meta.get("url", orig_url)

            # 크롤링 후 최종 URL 유효성 재검증
            # 1) 메인/카테고리 페이지 리다이렉트
            # 2) 구 URL이 만료돼 무관한 도메인으로 리다이렉트된 경우
            #    → 최종 URL이 방송사/OTT 도메인이어야만 허용
            # 3) Wayback Machine / 아카이브 URL로 리다이렉트된 경우 (명시적 거부)
            _redirect_invalid = (
                _is_archive_url(final_url)
                or _is_homepage_url(final_url)
                or not _is_known_drama_domain(final_url)
            )
            if _redirect_invalid:
                logging.info(f"[{drama_title}] 후보 URL 거부 (리다이렉트 무효): {orig_url} → {final_url}")

                # 원본 URL 자체가 드라마 도메인이면 폴백 후보로 보관
                # (리다이렉트 결과가 인식 불가여도 위키에서 공식 링크로 명시한 경우)
                if _is_known_drama_domain(orig_url) and not _is_homepage_url(orig_url) and not _is_archive_url(orig_url):
                    if fallback_url is None:
                        fallback_url = (orig_url, official_meta)

                # HTTP URL이 만료 도메인으로 리다이렉트된 경우 → HTTPS 버전으로 재시도
                if orig_url.startswith("http://"):
                    https_url = "https://" + orig_url[7:]
                    if https_url != orig_url:
                        logging.info(f"[{drama_title}] HTTPS 버전으로 재시도: {https_url}")
                        https_meta = await scrape_official_site(page, https_url)
                        https_final = https_meta.get("url", https_url)
                        if not _is_homepage_url(https_final) and _is_known_drama_domain(https_final):
                            logging.info(f"[{drama_title}] HTTPS 재시도 성공: {https_final}")
                            result["official_site_url"] = https_final
                            result["official_site"] = https_meta
                            result["metadata"] = _build_metadata(drama_title, wiki_data, https_meta)
                            found_valid = True
                            break
                continue

            result["official_site_url"] = final_url
            result["official_site"] = official_meta
            result["metadata"] = _build_metadata(drama_title, wiki_data, official_meta)
            found_valid = True
            break

        if not found_valid:
            # 폴백: 모든 후보가 리다이렉트 검증 실패했지만 원본 URL이 드라마 도메인인 경우
            if fallback_url:
                fb_url, fb_meta = fallback_url
                logging.info(f"[{drama_title}] 폴백 공식 사이트 URL 사용: {fb_url}")
                result["official_site_url"] = fb_url
                result["official_site"] = fb_meta
                result["metadata"] = _build_metadata(drama_title, wiki_data, fb_meta)
            else:
                result["error"] = "공식 사이트를 찾을 수 없습니다."
                result["official_site"] = {}   # 무효 리다이렉트 데이터 제거
                result["metadata"] = _build_metadata(drama_title, wiki_data, {})
    except Exception as e:
        result["error"] = str(e)
        result.setdefault("metadata", _build_metadata(drama_title, result.get("wikipedia", {}), {}))
    finally:
        if locals().get("_page_opened") and locals().get("page"):
            try:
                await page.close()
            except Exception:
                pass

    # ── poster 최종 보충: wiki_poster → official_site.poster 순으로 채움 ─────
    from crawlers.filters import is_logo_like_poster as _is_logo_p
    _meta = result.setdefault("metadata", {})
    if _meta.get("poster_url") and _is_logo_p(_meta["poster_url"]):
        _meta["poster_url"] = None      # 로고·배너는 포스터가 아니므로 비우고 아래 폴백을 계속 탄다
    if not _meta.get("poster_url"):
        # 1순위: 위키피디아 포스터
        _wp = result.get("wikipedia", {}).get("wiki_poster", "")
        # 2순위: 공식사이트 Playwright 스크랩 poster
        _op = result.get("official_site", {}).get("poster", "")
        _wp = "" if _is_logo_p(_wp) else _wp
        _op = "" if _is_logo_p(_op) else _op
        _meta["poster_url"] = _wp or _op or None

    # 공식사이트 poster가 여전히 없으면 httpx로 og:image 가져오기
    if not _meta.get("poster_url") and result.get("official_site_url"):
        try:
            import httpx as _hx_p
            _hdr_p = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9",
            }
            async with _hx_p.AsyncClient(headers=_hdr_p, follow_redirects=True, timeout=10) as _hc_p:
                _rp = await _hc_p.get(result["official_site_url"])
                _sp = BeautifulSoup(_rp.text, "lxml")
                _tp = _sp.find("meta", property="og:image")
                if _tp:
                    _img_p = _tp.get("content", "").strip()
                    # 한글 경로 URL 인코딩
                    _img_p = re.sub(
                        r'[가-힣ㄱ-ㅎㅏ-ㅣ]+',
                        lambda m: __import__('urllib.parse', fromlist=['quote']).quote(m.group(0), encoding='utf-8'),
                        _img_p,
                    )
                    if _img_p and not _is_logo_p(_img_p):
                        _meta["poster_url"] = _img_p
                        logging.info(f"[poster] 공식사이트 httpx og:image 보충: {_img_p!r}")
        except Exception as _pe:
            logging.debug(f"[poster] httpx poster 보충 실패: {_pe}")

    return result




# ── 독립 실행용 (CLI 테스트) ─────────────────────────────────────────────────

async def collect_drama_metadata(drama_title: str) -> dict:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ko-KR",
        )
        try:
            return await collect_drama_metadata_with_context(context, drama_title)
        finally:
            await browser.close()


if __name__ == "__main__":
    import sys
    title = sys.argv[1] if len(sys.argv) > 1 else "오징어 게임"
    data = asyncio.run(collect_drama_metadata(title))
    print(json.dumps(data, ensure_ascii=False, indent=2))
