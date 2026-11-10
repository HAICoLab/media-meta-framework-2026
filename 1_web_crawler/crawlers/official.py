"""방송사/OTT 공식 사이트에서 OG·JSON-LD·포스터·YouTube ID 수집."""

import json
import logging
import re
from playwright.async_api import Page
from bs4 import BeautifulSoup

# ── OG/메타 필드 필터 ────────────────────────────────────────────────────────
# 표시하지 않을 메타 키 (기술적·SEO 전용, 드라마 정보와 무관)
OG_EXCLUDE_KEYS = {
    "video", "video:secure_url", "video:type", "video:width", "video:height",
    "twitter:card", "twitter:site", "twitter:creator",
    "keywords",          # 방송사 공통 SEO 키워드라 드라마와 무관
    "fb:app_id", "fb:pages",
    "robots", "googlebot",
    "viewport", "theme-color",
}

# ── 공식 사이트 메타데이터 수집 ──────────────────────────────────────────────

async def scrape_official_site(page: Page, url: str) -> dict:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        return {"error": str(e), "url": url}

    html = await page.content()
    soup = BeautifulSoup(html, "lxml")
    meta = {"url": page.url}

    # ── OG / 메타 태그 (불필요 항목 제외) ────────────────────────────────────
    og = {}
    for tag in soup.select("meta"):
        prop = tag.get("property") or tag.get("name") or ""
        content = tag.get("content", "").strip()
        if not content:
            continue

        if prop.startswith("og:"):
            key = prop[3:]
        elif prop.startswith("twitter:"):
            key = f"twitter:{prop[8:]}"
        elif prop.lower() in ("description", "author"):
            key = prop.lower()
        else:
            continue

        # 불필요 키 제외
        if key in OG_EXCLUDE_KEYS:
            continue
        # 이미지 URL은 포함 (포스터로 사용)
        og[key] = content

    meta["og"] = og

    # ── <title> ──────────────────────────────────────────────────────────────
    title_tag = soup.find("title")
    meta["page_title"] = title_tag.get_text(strip=True) if title_tag else ""

    # ── JSON-LD 구조화 데이터 ────────────────────────────────────────────────
    jsonld_list = []
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "")
            jsonld_list.append(data)
        except Exception:
            pass
    meta["jsonld"] = jsonld_list

    # ── JSON-LD에서 핵심 정보 추출 ──────────────────────────────────────────
    structured = {}
    for jd in jsonld_list:
        if not isinstance(jd, dict):
            continue
        if jd.get("name"):
            structured["name"] = jd["name"]
        if jd.get("description"):
            structured["description"] = jd["description"]
        if jd.get("genre"):
            structured["genre"] = jd["genre"]
        if jd.get("dateCreated") or jd.get("startDate"):
            structured["date"] = jd.get("dateCreated") or jd.get("startDate")
        if jd.get("numberOfSeasons"):
            structured["seasons"] = str(jd["numberOfSeasons"])
        if jd.get("contentRating"):
            structured["rating"] = jd["contentRating"]
        if isinstance(jd.get("actors"), list):
            structured["actors"] = [a["name"] for a in jd["actors"] if isinstance(a, dict) and a.get("name")]
        if isinstance(jd.get("directors"), list):
            structured["directors"] = [d["name"] for d in jd["directors"] if isinstance(d, dict) and d.get("name")]
        if isinstance(jd.get("creators"), list):
            structured["creators"] = [c["name"] for c in jd["creators"] if isinstance(c, dict) and c.get("name")]
    meta["structured"] = structured

    # ── 텍스트 패턴 추출 ────────────────────────────────────────────────────
    text_body = soup.get_text(separator="\n")
    patterns = {
        "air_date":  r"(20\d{2}[.\-/년]\s*\d{1,2}[.\-/월]\s*\d{1,2}일?)",
        # 편성 시간: 요일(단일·범위·나열·일일) + 시간(HH:MM 또는 N시) 조합만 수집
        # 패턴 1: "월~금 오후 7:50", "월화수목금 저녁 7시"
        # 패턴 2: "매주 월, 화 밤 10시", "목요일 18:00"
        # 패턴 3: "매일 저녁 7시 50분"
        "schedule":  r"((?:매주\s*|매일\s*)?(?:(?:[월화수목금토일])+(?:[~\-·,\s]*[월화수목금토일])*|매일)\s*(?:요일)?\s*(?:밤|낮|오전|오후|저녁|아침)?\s*(?:\d{1,2}:\d{2}|\d{1,2}시)(?:\s*\d{1,2}분)?)",
        "channel":   r"(KBS\s*\d?|MBC|SBS|tvN|JTBC|OCN|MBN|채널A|TV조선|ENA|Disney\+|Netflix|웨이브|왓챠|티빙)",
        "episodes":  r"(\d+)\s*(?:부작|회|편(?!수)|episode)",
        "director":  r"(?:감독|연출)[:\s]+([가-힣]{2,6})",
        "writer":    r"(?:작가|극본)[:\s]+([가-힣]{2,6})",
        "cast":      r"(?:주연|출연)[:\s]+([가-힣, ]{4,30})",
    }
    extracted = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, text_body)
        if matches:
            seen_m: list = []
            for m in matches:
                m = m.strip()
                if m and m not in seen_m:
                    seen_m.append(m)
            extracted[key] = seen_m[:5]
    meta["extracted"] = extracted

    # ── 포스터 이미지 ────────────────────────────────────────────────────────
    # 1순위: og:image / twitter:image
    # 2순위: JSON-LD image
    # 3순위: <img> 태그 중 포스터/썸네일로 보이는 것
    _poster = (
        og.get("image")
        or og.get("twitter:image")
        or structured.get("image")
    )
    # 로고·파비콘·사이트 공용 이미지를 포스터로 쓰지 않도록 필터
    # (banner 는 드라마 홍보 이미지일 수 있으므로 제외하지 않음)
    _LOGO_SKIP = re.compile(
        r"logo|favicon|icon|badge|bg\b|background|sprite|symbol|common"
        r"|_home\.|home_|/og/|_og\.|og_image|og-image|ogimage"
        r"|default|placeholder|blank|dummy|noimage|no_image"
        r"|site_image|main_og|index_og",
        re.IGNORECASE,
    )

    def _poster_src_ok(src: str) -> bool:
        """로고·파비콘·사이트 공통 og 이미지 등 포스터로 부적합한 이미지 걸러냄"""
        if not src or src.startswith("data:"):
            return False
        if _LOGO_SKIP.search(src):
            return False
        return True

    # og:image / twitter:image가 로고라면 무시
    if _poster and not _poster_src_ok(_poster):
        _poster = None

    # 상대경로 → 절대경로 변환 헬퍼
    def _abs(src: str) -> str:
        if src.startswith("http"):
            return src
        if src.startswith("//"):
            return "https:" + src
        if src.startswith("/"):
            from urllib.parse import urlparse as _up2
            base = _up2(url)
            return f"{base.scheme}://{base.netloc}{src}"
        return src

    if not _poster:
        # alt/class/src에 poster·thumbnail·main·key 키워드가 있는 img 우선
        for img in soup.select("img[src]"):
            src = img.get("src", "")
            alt = img.get("alt", "").lower()
            cls = " ".join(img.get("class") or []).lower()
            if not _poster_src_ok(src):
                continue
            if any(kw in alt or kw in cls or kw in src.lower()
                   for kw in ["poster", "thumbnail", "main", "key", "title", "cover", "visual"]):
                _poster = _abs(src)
                break

    # ── 경로 기반 폴백: /data/file/, /upload/, /files/, /img/drama 등 ─────────
    # og:image·키워드 이미지 모두 실패한 경우 컨텐츠 이미지 경로로 재시도
    _CONTENT_PATH = re.compile(
        r"/data/file/|/upload/|/files/drama|/contents/drama|/img/drama"
        r"|/board/|/bbs/.*\.(jpg|jpeg|png|webp)"
        r"|/relatedmaterial|/original/poster|/original/thumb"
        r"|/pcms/photogroup/|/pcms/character/",   # CJ ENM 드라마별 이미지
        re.IGNORECASE,
    )
    if not _poster:
        for img in soup.select("img[src]"):
            src = img.get("src", "")
            if not src or not _poster_src_ok(src):
                continue
            # 로고/SNS/버튼 경로 제외
            low = src.lower()
            if any(skip in low for skip in ["logo", "sns_", "/btn", "icon", "no_profile", "no_img"]):
                continue
            if _CONTENT_PATH.search(src):
                _poster = _abs(src)
                logging.info(f"[scrape_official] 경로 기반 포스터 폴백: {_poster!r}")
                break
    # ── Playwright가 SPA에서 og:image를 못 가져왔으면 httpx로 fallback ────────
    if not _poster:
        try:
            import httpx as _hx_fallback
            _hdr_fb = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9",
            }
            _HTTPX_KW = re.compile(
                r"poster|thumbnail|cover|visual|key.*art|main.*img|title.*img"
                r"|/poster/|/original/poster|/data/file/drama|/relatedmaterial",
                re.IGNORECASE,
            )
            async def _httpx_find_img(u: str) -> str:
                async with _hx_fallback.AsyncClient(headers=_hdr_fb, follow_redirects=True, timeout=10) as _c:
                    _res = await _c.get(u)
                    _s = BeautifulSoup(_res.text, "lxml")

                    def _abs_u(src: str) -> str:
                        if src.startswith("http"): return src
                        if src.startswith("//"): return "https:" + src
                        if src.startswith("/"):
                            from urllib.parse import urlparse as _up3
                            _b = _up3(u)
                            return f"{_b.scheme}://{_b.netloc}{src}"
                        return src

                    # 1순위: og:image
                    _t = _s.find("meta", property="og:image")
                    if _t:
                        _img = _t.get("content", "").strip()
                        if _img and _poster_src_ok(_img):
                            return _img
                    # 2순위: src에 poster/thumbnail/경로 키워드 포함 img
                    for _img_tag in _s.select("img[src]"):
                        _src = _img_tag.get("src", "")
                        _alt = _img_tag.get("alt", "").lower()
                        _cls = " ".join(_img_tag.get("class") or []).lower()
                        if not _src or not _poster_src_ok(_abs_u(_src)):
                            continue
                        if any(skip in _src.lower() for skip in ["logo", "sns_", "/btn", "icon", "no_profile", "no_img", "/icon", "32x32", "favicon"]):
                            continue
                        if _HTTPX_KW.search(_src) or _HTTPX_KW.search(_alt) or _HTTPX_KW.search(_cls):
                            return _abs_u(_src)
                return ""
            _img_url = await _httpx_find_img(url)
            if _img_url:
                _poster = _img_url
                logging.info(f"[scrape_official] httpx fallback 이미지: {_img_url!r}")
        except Exception as _fe:
            logging.debug(f"[scrape_official] httpx fallback 실패: {_fe}")

    meta["poster"] = _poster or ""

    # ── 스트리밍 서비스 링크 (주요 URL만, 중복 제거) ─────────────────────────
    STREAMING_SVCS = ["wavve", "tving", "netflix", "disney", "viki", "viu", "watcha"]
    seen_svc: set = set()
    streaming = []
    for a in soup.select("a[href]"):
        href = a["href"]
        for svc in STREAMING_SVCS:
            if svc in href.lower() and svc not in seen_svc:
                skip_parts = ["help.", "/legal", "/jobs", "/login", "/browse", "/contactus", "/support"]
                if not any(p in href.lower() for p in skip_parts):
                    seen_svc.add(svc)
                    streaming.append({"service": svc, "url": href})
                break
    meta["streaming"] = streaming

    # ── verified_characters: JSON-LD actor 배열에서 배우-역할 추출 ─────────────
    verified_characters = []
    for jd in jsonld_list:
        if not isinstance(jd, dict):
            continue
        for actor_obj in jd.get("actors", []):
            if not isinstance(actor_obj, dict):
                continue
            name = actor_obj.get("name", "")
            role = (actor_obj.get("characterName") or
                    actor_obj.get("character") or
                    (actor_obj.get("hasOccupation", {}) or {}).get("name") or "")
            if name:
                verified_characters.append({"actor": name, "character": role})
    meta["verified_characters"] = verified_characters

    # ── YouTube 영상 ID 감지 (iframe embed / <a href> / img 썸네일) ──────────
    _yt_re   = re.compile(r"(?:youtube\.com/(?:embed|watch|v|shorts)/|youtu\.be/)([A-Za-z0-9_-]{11})")
    # img.youtube.com/vi/VIDEO_ID/... 패턴 (ktena.co.kr 등에서 썸네일만 img로 사용하는 경우)
    _yt_thumb = re.compile(r"img\.youtube\.com/vi/([A-Za-z0-9_-]{11})/")
    seen_yt: list = []

    # 1. BeautifulSoup (정적 HTML)
    for iframe in soup.select("iframe[src]"):
        m = _yt_re.search(iframe.get("src", ""))
        if m and m.group(1) not in seen_yt:
            seen_yt.append(m.group(1))
    for a in soup.select("a[href]"):
        m = _yt_re.search(a.get("href", ""))
        if m and m.group(1) not in seen_yt:
            seen_yt.append(m.group(1))
    # img 태그 YouTube 썸네일에서 ID 추출
    for img in soup.select("img[src]"):
        m = _yt_thumb.search(img.get("src", ""))
        if m and m.group(1) not in seen_yt:
            seen_yt.append(m.group(1))

    # 2. Playwright 동적 감지 (SPA / lazy-load 대응)
    try:
        dynamic_ids = await page.evaluate(r"""() => {
            const re = /(?:youtube\.com\/(?:embed|watch|v|shorts)\/|youtu\.be\/)([A-Za-z0-9_-]{11})/;
            const ids = [];
            for (const el of document.querySelectorAll('iframe[src], a[href*="youtube"], a[href*="youtu.be"]')) {
                const src = el.src || el.href || '';
                const m = src.match(re);
                if (m) ids.push(m[1]);
            }
            return [...new Set(ids)];
        }""")
        for vid in (dynamic_ids or []):
            if vid not in seen_yt:
                seen_yt.append(vid)
    except Exception:
        pass

    meta["youtube_ids"] = seen_yt[:10]  # 최대 10개

    return meta
