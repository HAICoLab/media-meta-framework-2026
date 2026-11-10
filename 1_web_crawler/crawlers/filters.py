"""포스터·공식사이트 URL 필터 (로고 제외, Netflix 차단, 후보 순위)."""

import re
from typing import Optional

# ── 사이트 공통 배너/로고 이미지 URL 필터 ─────────────────────────────────────
_POSTER_LOGO_RE = re.compile(
    r"logo|favicon|badge"
    r"|브랜딩|sitemng|systemmng|gnbmng"        # tvN/CJ ENM 사이트 관리 이미지
    r"|대체이미지"                              # CJ ENM 대체 이미지
    r"|sns.{0,10}배너|배너.{0,10}sns"          # SNS 공유 배너 (한국어)
    r"|sns.{0,10}share.{0,10}banner|share.{0,10}banner"  # SNS 공유 배너 (영문)
    r"|_og\.|og_image|ogimage|og-image"        # 일반 og 기본 이미지
    r"|[/_-]default\.|/default$"               # default 이미지 (mqdefault 제외: 앞에 [/_-] 필요)
    r"|placeholder|noimage|no_image|no_img|no-image"
    r"|site_image|main_og|index_og",
    re.IGNORECASE,
)

def is_logo_like_poster(url: str) -> bool:
    """드라마 포스터가 아닌 사이트 공통 배너/로고 이미지이면 True"""
    if not url:
        return True
    # YouTube 썸네일은 항상 허용
    if "img.youtube.com/vi/" in url:
        return False
    return bool(_POSTER_LOGO_RE.search(url))

# ── 공식 사이트 후보 선택 ────────────────────────────────────────────────────

OFFICIAL_KEYWORDS = [
    "공식", "official", "drama", "tv.naver", "tv.kakao",
    "wavve", "tving", "seezn", "kbs.co.kr", "imbc.com", "sbs.co.kr",
    "tvn.com", "jtbc", "ocn", "mbn", "channel", "studio",
]
NON_OFFICIAL = [
    "//twitter.com", "//x.com", "//www.x.com", "//t.co/",
    "//facebook.com", "//www.facebook.com",
    "//instagram.com", "//www.instagram.com",
    "//youtube.com", "//www.youtube.com",
    "namu.wiki", "naver.com/search",
    "//google.com", "//www.google.com",
    "//imdb.com", "//www.imdb.com",
    "mydramalist", "asianwiki", "hancinema",
    "wikipedia.org", "wikimedia", "wikidata",
    # 아카이브/캐시 서비스 — 원본 URL이 내부에 포함돼도 공식사이트로 인정하지 않음
    "web.archive.org", "archive.org", "waybackmachine", "ghostarchive",
    "perma.cc", "webcache.googleusercontent", "timetravel.mementoweb",
    "//variety.com", "//theguardian.com",
    "viaf.org", "d-nb.info", "loc.gov", "catalogue.bnf", "data.bnf",
    "openlibrary.org", "aleph.nkp", "catalogo.bne", "dbn.bn.org",
    "cantic.bnc",
    # 방송사 뉴스/보도 서브도메인 (공식 드라마 페이지 아님)
    "news.jtbc", "news.kbs", "news.sbs", "news.mbc",
    "entertain.naver.com", "nme.com", "hankyung.com", "tenasia",
]


_ARCHIVE_DOMAINS = [
    "web.archive.org", "archive.org", "waybackmachine", "ghostarchive.org",
    "perma.cc", "webcache.googleusercontent", "timetravel.mementoweb",
]


def _is_archive_url(url: str) -> bool:
    """Wayback Machine / 아카이브 서비스 URL 여부를 판별합니다."""
    url_lower = url.lower()
    return any(d in url_lower for d in _ARCHIVE_DOMAINS)


def _is_netflix_url(url: str) -> bool:
    """Netflix 계열 URL 여부. Netflix 는 수집이 불가능하므로 어떤 경로로도 통과시키지 않는다.

    호스트 이름 어디에든 'netflix' 가 들어 있으면 차단한다
    (netflix.com, www.netflix.com, netflix.com.evil.com, evil-netflix.com 등).
    """
    try:
        from urllib.parse import urlparse
        raw = (url or "").strip()
        if "://" not in raw:
            raw = "//" + raw          # 스킴 없는 입력('netflix.com/kr/...')도 호스트로 파싱되게
        host = (urlparse(raw).hostname or "").lower()
        return "netflix" in host
    except Exception:
        return "netflix" in (url or "").lower()


def _is_known_drama_domain(url: str) -> bool:
    """리다이렉트 후 최종 URL이 방송사/OTT/드라마 관련 도메인인지 검증.
    구 URL이 만료돼 전혀 무관한 사이트로 연결되는 경우를 차단.
    Wayback Machine / 아카이브 URL은 항상 False 처리 (원본 URL이 포함돼도 무시).
    Netflix 는 수집 불가이므로 항상 False.
    """
    # 아카이브 서비스 URL은 원본 URL 포함 여부와 무관하게 거부
    if _is_archive_url(url):
        return False
    if _is_netflix_url(url):
        return False

    _DRAMA_KEYWORDS = [
        "kbs", "mbc", "sbs", "jtbc", "tvn", "ocn", "mbn",
        "chosun", "channela", "ichannela", "ena", "skylifetv",
        "interest.me", "seezn", "tving", "wavve",
        "disneyplus", "watcha", "laftel", "kakao", "naver",
        "cjenm", "studio", "drama", "program", "tv.",
        "mbccb", "com2usn", "com2us", "wiziwig",
    ]
    url_lower = url.lower()
    return any(kw in url_lower for kw in _DRAMA_KEYWORDS)


def _is_homepage_url(url: str) -> bool:
    """방송사/OTT 메인 페이지 여부 판별 (드라마 전용 페이지가 아닌 경우).

    경로가 없거나 1단계 일반 카테고리 이하이면 메인 페이지로 간주.
    예) https://www.sbs.co.kr          →  True  (0 segments)
        https://programs.sbs.co.kr/drama/  →  True  (일반 카테고리)
        https://tv.jtbc.co.kr/HeavenlyEverAfter  →  False  (드라마 고유 slug)
        https://programs.sbs.co.kr/drama/lovescout/about/84255  →  False
    """
    # 1단계 경로라도 드라마 고유 slug이면 드라마 페이지로 인정
    _GENERIC_SEGS = {
        "drama", "dramas", "tv", "main", "index", "program", "programs",
        "movie", "movies", "series", "home", "about", "news", "enter",
    }
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        # 경로 세그먼트 중 비어있지 않은 것
        segs = [s for s in parsed.path.split("/") if s]
        # 경로 없음 → 확실히 메인
        if len(segs) == 0:
            return True
        # 1단계이고 일반 카테고리 키워드 → 카테고리/메인 페이지
        if len(segs) == 1 and segs[0].lower() in _GENERIC_SEGS:
            return True
        # 1단계이고 순수 숫자 1~4자리 → 페이지 인덱스일 가능성
        if len(segs) == 1 and segs[0].isdigit() and len(segs[0]) <= 4:
            return True
    except Exception:
        pass
    return False


def pick_official_site(external_links: list[dict]) -> Optional[dict]:
    """가장 적합한 공식 사이트 후보 1개를 반환 (내부적으로 rank_official_sites 사용)"""
    ranked = rank_official_sites(external_links)
    return ranked[0] if ranked else None


def rank_official_sites(external_links: list[dict]) -> list[dict]:
    """공식 사이트 후보를 점수 순으로 정렬해 반환.

    우선순위:
      1. OFFICIAL_KEYWORDS 기반 점수 (방송사 도메인, 공식/official 등 텍스트)
      2. 방송사/OTT 도메인 여부 (_is_known_drama_domain → +3)
      3. HTTPS 우선 (+0.5, 동점 때만 영향)
      웹툰 플랫폼 링크(링크 텍스트에 '웹툰' 포함 또는 웹툰 도메인)는 점수 차감
    """
    # 웹툰 링크 판별 패턴
    WEBTOON_DOMAINS = ["webtoon.", "webtoon/", "page.kakao.com", "daum.net/m/webtoon", "comic.", "manhwa"]
    WEBTOON_TEXTS = ["웹툰", "webtoon", "manhwa", "comic"]

    scored = []
    for lnk in external_links:
        url = lnk["url"]
        url_lower = url.lower()
        text_lower = lnk["text"].lower()

        if _is_netflix_url(url):      # Netflix 는 수집 불가 → 공식 사이트 후보에서 제외
            continue

        if any(kw in url_lower for kw in NON_OFFICIAL):
            continue

        # 방송사 메인/카테고리 페이지는 공식사이트로 인정하지 않음
        if _is_homepage_url(url):
            continue

        # 웹툰 사이트/페이지: 드라마 공식 사이트 아님 → 대폭 감점
        is_webtoon = (
            any(kw in url_lower for kw in WEBTOON_DOMAINS)
            or any(kw in text_lower for kw in WEBTOON_TEXTS)
        )
        if is_webtoon:
            continue  # 웹툰 링크는 아예 제외

        score = 0
        for kw in OFFICIAL_KEYWORDS:
            if kw in url_lower or kw in text_lower:
                score += 2  # 키워드 매칭마다 2점

        # 방송사/OTT 도메인이면 큰 보너스
        if _is_known_drama_domain(url):
            score += 3

        # 링크 텍스트에 "공식" 포함 시 추가 보너스 (위키 인포박스의 "공식 홈페이지" 등)
        if "공식" in text_lower or "official" in text_lower:
            score += 2

        # OTT: 한국 지역코드(/kr/) URL 우선 (+1)
        if "tving.com" in url_lower or "wavve.com" in url_lower:
            if "/kr/" in url_lower or "/ko/" in url_lower:
                score += 1

        # HTTPS 우선 (미세 보너스 — 동점일 때만 순서 결정)
        https_bonus = 0.5 if url.startswith("https://") else 0

        scored.append((score + https_bonus, lnk))

    # 총점 내림차순
    scored.sort(key=lambda x: -x[0])
    return [item[1] for item in scored]
