"""위키피디아 문서 HTML 파싱 (인포박스, 섹션, 외부링크, 동음이의어)."""

import logging
import re
from typing import Optional
from bs4 import BeautifulSoup

from crawlers.filters import pick_official_site

class WikiRateLimitError(Exception):
    """위키피디아 API 429 Too Many Requests — Dumps 방식으로 전환 필요"""
    pass

# ── 동음이의어(disambiguation) 처리 ─────────────────────────────────────────

# 드라마 문서를 가리키는 URL/텍스트 키워드
DRAMA_URL_KW  = ["드라마", "tv_시리즈", "tv시리즈", "텔레비전", "시트콤", "웹드라마",
                 "미니시리즈", "드라마_시리즈", "연속극"]
# 드라마가 아닌 항목 제외 키워드
NON_DRAMA_KW  = ["영화", "게임", "음반", "앨범", "소설", "만화", "애니", "골프", "운전사",
                 "배우", "장치", "소프트웨어", "스포츠", "스크류"]


def _clean_infobox_val(val: str) -> str:
    """인포박스 값 정리: 각주·위키 이미지 마크업 잔여물 제거, 연속 공백 정리"""
    if not val:
        return ""
    # 1) 완전한 위키 파일 링크: [[파일:xxx|40px|link=채널명]] → 채널명 (link= 없으면 제거)
    def _repl_file(m):
        s = m.group(0)
        lm = re.search(r"\|link=([^\]|]+)", s)
        return lm.group(1).strip() if lm else ""
    val = re.sub(r"\[\[(?:파일|File|이미지|Image):[^\]]*?\]\]", _repl_file, val, flags=re.IGNORECASE)
    # 2) 파싱 후 남은 이미지 마크업 잔여물 처리
    #    케이스 A: 앞에 실제 텍스트가 있는 경우
    #       "모래시계 20px|15세 이상 시청가|link=..." → "모래시계"
    val = re.sub(r"\s+\d+px\s*\|[^|\n]*\|link=[^\n,]*", "", val)
    #    케이스 B: 전체가 이미지 크기+link 잔여물인 경우
    #       "40px|link=SBS TV" → "SBS TV"
    _lm = re.match(r"^\d+px\s*\|(?:[^|]*\|)*link=(.+)$", val.strip())
    if _lm:
        val = _lm.group(1).strip()
    # 3) 일반 위키링크: [[링크|텍스트]] → 텍스트
    val = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", val)
    # 4) 각주 제거
    val = re.sub(r"\[\s*\d+\s*\]", "", val)
    return tidy_text(val)   # 연속 공백 + '김 , 이' / '( MBC )' 같은 어색한 공백 정리

def _is_disambiguation(soup: BeautifulSoup) -> bool:
    """동음이의어 페이지 여부 판별"""
    # 1) CSS 클래스로 판별
    if soup.select_one(".dmbox, #disambigbox, .disambiguation"):
        return True
    # 2) 헤드노트 텍스트로 판별
    for tag in soup.select(".hatnote, .mw-message-box, .dablink"):
        if "동음이의어" in tag.get_text() or "disambiguation" in tag.get_text().lower():
            return True
    # 3) 본문 첫 줄에 "다음 뜻이 있다" 패턴
    content = soup.select_one("#mw-content-text .mw-parser-output")
    if content:
        first_text = content.get_text()[:200]
        if "다음 뜻이 있다" in first_text or "다음과 같다" in first_text:
            return True
    return False


def _find_drama_link_in_disambiguation(soup: BeautifulSoup, hint: str = "") -> Optional[str]:
    """동음이의어 페이지에서 드라마 문서 URL 선택"""
    content = soup.select_one("#mw-content-text")
    if not content:
        return None

    candidates: list[tuple[int, str]] = []  # (score, url)

    for a in content.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(strip=True)

        # 절대 URL로 정규화
        if href.startswith("/wiki/"):
            url = "https://ko.wikipedia.org" + href
        elif href.startswith("https://ko.wikipedia.org"):
            url = href
        else:
            continue

        # 특수 페이지 제외
        if any(p in url for p in ["/특수:", "/도움말:", "/위키백과:", "oldid=", "action="]):
            continue

        # ── 비드라마 제외: URL/텍스트의 괄호 접미사 단어만 체크 ──────────────────
        # "_(영화)", "_(게임)" 같은 접미사로만 판단.
        # 제목 자체에 해당 단어가 들어간 드라마(머니게임, 신의게임...)를 오탈락 방지.
        url_suffix = re.search(r"_\(([^)]+)\)$", url)
        suffix_word = url_suffix.group(1) if url_suffix else ""
        text_bracket = re.search(r"\(([^)]+)\)$", text)
        bracket_word = text_bracket.group(1) if text_bracket else ""

        # 괄호 안 분류 단어만 비드라마 체크 (제목 전체 제외)
        exclude_check = (suffix_word + " " + bracket_word).lower()
        if any(kw in exclude_check for kw in NON_DRAMA_KW):
            continue

        # ── 드라마 키워드 점수 ───────────────────────────────────────────────────
        # a 태그 부모 li의 전체 텍스트도 포함 (예: "《도깨비》: 한국의 드라마")
        parent_li = a.find_parent("li")
        parent_text = parent_li.get_text(strip=True) if parent_li else ""
        combined = (url + " " + text + " " + parent_text).lower()
        score = sum(1 for kw in DRAMA_URL_KW if kw in combined)
        if score == 0:
            continue

        # ── URL 접미사로 "명확한 드라마 문서" 가산점 ────────────────────────────
        # _(드라마) 또는 _(2022년_드라마) 등 접미사가 있으면 강한 신호
        if re.search(r"_\((\d{4}년_)?드라마\)$", url):
            score += 5

        # ── 방송사/프로그램 패키지 링크 제외 ──────────────────────────────────
        # "KBS 드라마게임", "MBC 드라마" 등 방송사 페이지는 드라마 개별 문서가 아님
        BROADCASTER_PREFIX = ["KBS", "MBC", "SBS", "tvN", "JTBC", "OCN", "채널", "EBS", "방송"]
        if any(text.startswith(bp) for bp in BROADCASTER_PREFIX):
            continue

        # ── 한국 드라마 가산점 / 외국 리메이크 감점 ──────────────────────────────
        # 부모 li 텍스트나 URL 괄호 안에 한국 방송사 · 국적 언급이 있으면 +3
        KR_SIGNALS = ["대한민국", "한국", "kbs", "mbc", "sbs", "tvn", "jtbc", "ocn", "채널a", "ebs"]
        # 외국 리메이크 신호 — 특정 국가명이 괄호 접미사에 있으면 -5
        FOREIGN_SIGNALS = ["필리핀", "중국", "미국", "일본", "태국", "베트남", "인도", "터키",
                           "philippine", "china", "thai", "vietnam", "indian", "turkish"]
        score += sum(2 for kw in KR_SIGNALS if kw in combined)
        if any(kw in (suffix_word + " " + bracket_word + " " + parent_text).lower()
               for kw in FOREIGN_SIGNALS):
            score -= 5

        # ── 연도 추출 (동점 시 최신 선택에 사용) ────────────────────────────────
        year_match = re.search(r"(\d{4})년", url + " " + text)
        year = int(year_match.group(1)) if year_match else 0

        candidates.append((score, year, url))

    if not candidates:
        return None

    # score 내림차순, 동점 시 연도 내림차순(최신 우선)으로 정렬
    candidates.sort(key=lambda x: (-x[0], -x[1]))
    return candidates[0][2]


# ── 위키피디아 단일 문서 파싱 ────────────────────────────────────────────────

_WIKI_HEADERS = {
    # Wikipedia 봇 정책 준수 User-Agent (연락처 포함)
    "User-Agent": "KDramaResearch/1.0 (Academic Research; admin@example.com)",
}

_WIKI_API = "https://ko.wikipedia.org/w/api.php"

# Wikipedia API를 urllib로 직접 호출하는 헬퍼 (httpx가 403 차단되므로 urllib 사용)
_WIKI_URLLIB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

_wiki_last_request_time: float = 0.0
_wiki_min_interval: float = 2.0  # Wikipedia API 최소 요청 간격 (초) — 429 방지

def _urllib_get_json(url: str) -> dict | None:
    """urllib로 URL에 GET 요청 후 JSON 파싱. 429 Rate Limit 시 자동 재시도.
    Wikipedia 정책 준수: 요청 간 최소 1초 간격 유지."""
    import urllib.request as _ur
    import urllib.error as _ue
    import gzip as _gz
    import time as _time
    global _wiki_last_request_time

    for attempt in range(3):
        # Wikipedia 최소 요청 간격 준수
        elapsed = _time.time() - _wiki_last_request_time
        if elapsed < _wiki_min_interval:
            _time.sleep(_wiki_min_interval - elapsed)

        try:
            req = _ur.Request(url, headers=_WIKI_URLLIB_HEADERS)
            _wiki_last_request_time = _time.time()
            with _ur.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                ct = resp.headers.get("Content-Encoding", "")
                if ct == "gzip":
                    raw = _gz.decompress(raw)
                return __import__("json").loads(raw)
        except _ue.HTTPError as _he:
            if _he.code == 429:
                wait = min(int(_he.headers.get("Retry-After", 10)), 30)  # 최대 30초 대기
                logging.warning(f"[wiki_urllib] 429 Rate Limit → {wait}초 대기 (attempt {attempt+1})")
                _time.sleep(wait)
                continue
            logging.warning(f"[wiki_urllib] 요청 실패 {url}: {_he}")
            return None
        except Exception as _e:
            logging.warning(f"[wiki_urllib] 요청 실패 {url}: {_e}")
            if attempt < 2:
                _time.sleep(3 * (attempt + 1))
                continue
            return None
    return None

async def _wiki_get_json(url: str) -> dict | None:
    """urllib로 Wikipedia JSON API 호출 (opensearch 등 가벼운 API 전용)."""
    import asyncio as _aio
    return await _aio.to_thread(_urllib_get_json, url)


def _safe_url(url: str) -> str:
    """비ASCII 문자만 퍼센트 인코딩 (이미 인코딩된 %XX 와 URL 구분자는 보존)."""
    import urllib.parse as _up
    return _up.quote(url, safe="%/:?&=#@!$'()*+,;~[]")


def _urllib_fetch_html(url: str) -> tuple[str, str]:
    """Wikipedia 페이지 HTML을 직접 긁어옵니다. (html, final_url) 반환.
    API 불필요 — 일반 브라우저처럼 접근하므로 Rate Limit 없음."""
    import urllib.request as _ur
    import urllib.error as _ue
    import gzip as _gz
    import zlib as _zlib
    # Accept-Encoding 없이 요청 → 압축 없이 plain HTML 수신
    _plain_headers = {k: v for k, v in _WIKI_URLLIB_HEADERS.items()
                      if k.lower() != "accept-encoding"}
    # 한글 등 비ASCII 문자가 인코딩 없이 들어와도 요청되도록 정규화
    # (이미 %XX 로 인코딩된 부분은 그대로 유지)
    url = _safe_url(url)
    try:
        req = _ur.Request(url, headers=_plain_headers)
        with _ur.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            enc = resp.headers.get("Content-Encoding", "")
            if enc == "gzip":
                raw = _gz.decompress(raw)
            elif enc in ("deflate", "zlib"):
                raw = _zlib.decompress(raw)
            html = raw.decode("utf-8", errors="replace")
            return html, resp.url
    except _ue.HTTPError as _he:
        logging.warning(f"[wiki_html] HTTP {_he.code} {url}")
        return "", url
    except Exception as _e:
        logging.warning(f"[wiki_html] 실패 {url}: {_e}")
        return "", url


def _dump_has_title(title: str) -> bool | None:
    """로컬 dumps 인덱스에 이 제목의 드라마/예능 문서가 있는지. 인덱스가 없거나 조회 실패면 None(판단 불가)."""
    import sqlite3 as _sq
    from core.paths import WIKI_INDEX as _idx
    if not _idx.exists():
        return None
    try:
        con = _sq.connect(f"file:{_idx}?mode=ro", uri=True)
        try:
            return con.execute("SELECT 1 FROM wiki_pages WHERE title=? LIMIT 1", (title,)).fetchone() is not None
        finally:
            con.close()
    except Exception as _e:
        logging.debug(f"[wiki_html] dump 제목 조회 실패 ({title}): {_e}")
        return None


async def _wiki_fetch_html(url: str) -> tuple[str, str]:
    """비동기로 Wikipedia HTML 직접 페이지 가져오기."""
    import asyncio as _aio
    return await _aio.to_thread(_urllib_fetch_html, url)


def _parse_categories_from_html(soup) -> list[str]:
    """Wikipedia HTML에서 분류 목록 파싱."""
    cats = []
    cat_div = soup.select_one("#mw-normal-catlinks ul, .mw-normal-catlinks ul")
    if cat_div:
        for a in cat_div.select("a"):
            title = a.get("title", "").replace("분류:", "").strip()
            if title:
                cats.append(title)
    return cats


def _parse_meta_from_html(soup, page_url: str) -> dict:
    """Wikipedia HTML에서 메타 정보(missing/disambig/categories/redirect) 파싱."""
    html_str = str(soup)
    # 없는 문서 감지
    missing = (
        "존재하지 않습니다" in html_str
        or "noarticletext" in html_str
        or soup.select_one(".noarticletext") is not None
    )
    categories = _parse_categories_from_html(soup)
    # 동음이의어 감지
    # 문서 전체에서 "위키백과:동음이의" 문자열을 찾으면 안 된다: 일반 문서 상단 hatnote
    # ("다른 뜻은 동음이의어 문서 참조")에도 같은 링크가 있어 본문 문서가 오탐된다
    # (예: 태양의 후예 → 본문 링크 '2016년'으로 이동해 거부됨).
    # 실제 동음이의어 문서는 .dmbox-disambig 박스 + '동음이의어 문서' 카테고리를 가진다.
    is_disambig = bool(
        soup.select_one(".dmbox-disambig, .dmbox.dmbox-disambig")
        or any("동음이의어" in c for c in categories)
    )
    # 리다이렉트 감지
    redirect_to = None
    redirect_div = soup.select_one(".redirectMsg a, #redirectsub + p a")
    if redirect_div:
        redirect_to = redirect_div.get("title", "").strip() or None

    return {
        "missing": missing,
        "is_disambiguation": is_disambig,
        "categories": categories,
        "redirect_to": redirect_to,
    }


async def _wiki_api_get_html(page_title: str, client, _redirect_depth: int = 0,
                             _with_ext: bool = False) -> tuple[str, str] | tuple[str, str, list]:
    """Wikipedia 페이지 HTML을 직접 가져옵니다 (API 불필요).
    _with_ext=False: (html, page_url) 반환
    _with_ext=True:  (html, page_url, ext_links=[]) 반환"""
    import urllib.parse as _up
    page_url = f"https://ko.wikipedia.org/wiki/{_up.quote(page_title.replace(' ', '_'))}"
    empty_ret = ("", page_url, []) if _with_ext else ("", page_url)

    html, final_url = await _wiki_fetch_html(page_url)
    if not html:
        return empty_ret

    # 리다이렉트 감지
    if _redirect_depth < 3:
        from bs4 import BeautifulSoup as _BS
        soup_r = _BS(html, "lxml")
        redirect_link = soup_r.select_one(".redirectMsg a")
        if redirect_link:
            redirect_title = redirect_link.get("title", "").strip()
            if redirect_title and redirect_title != page_title:
                return await _wiki_api_get_html(redirect_title, client, _redirect_depth + 1, _with_ext=_with_ext)

    if _with_ext:
        return html, final_url, []  # 외부링크는 HTML 파싱에서 직접 추출
    return html, final_url


async def _wiki_query_meta(page_title: str, client) -> dict:
    """Wikipedia 페이지 HTML에서 메타 정보를 가져옵니다 (API 불필요).
    반환: {"missing", "is_disambiguation", "categories", "redirect_to"}
    """
    import urllib.parse as _up
    _empty = {"missing": False, "is_disambiguation": False, "categories": [], "redirect_to": None}
    page_url = f"https://ko.wikipedia.org/wiki/{_up.quote(page_title.replace(' ', '_'))}"
    html, _ = await _wiki_fetch_html(page_url)
    if not html:
        return _empty
    from bs4 import BeautifulSoup as _BS
    soup = _BS(html, "lxml")
    return _parse_meta_from_html(soup, page_url)


# ── 카테고리 기반 판별 헬퍼 ────────────────────────────────────────────────────

_DRAMA_CAT_KW  = ["텔레비전 드라마", "TV 드라마", "드라마 시리즈", "웹드라마", "텔레비전 시리즈",
                   "텔레비전_드라마", "TV_드라마", "드라마화", "시트콤"]
# ※ "텔레비전 프로그램", "방송 프로그램"은 제거 — 애니메이션·예능도 포함하므로 오인 방지
_NON_DRAMA_CAT_KW = [
    # 출판/그림
    "웹툰", "만화", "소설", "웹소설", "수필", "시집",
    # 영상
    "영화", "film", "movie", "애니메이션", "애니메", "anime",
    # 음악
    "음반", "음악 그룹", "밴드", "가수", "뮤지션", "싱글", "앨범", "음악가",
    "결성된 음악", "해체된 음악", "록 밴드", "보이밴드", "걸그룹", "아이돌 그룹",
    # 게임
    "게임", "비디오 게임", "모바일 게임",
    # 기타
    "라디오 프로그램", "팟캐스트",
    # 인물 (드라마 제목이 인명과 겹치는 경우 방지는 별도 로직)
    "스포츠 선수", "운동선수", "정치인", "기업인",
]

_VARIETY_CAT_KW = [
    "예능 프로그램", "버라이어티", "오락 프로그램", "토크쇼", "리얼리티",
    "관찰 예능", "쿠킹 프로그램", "교양 프로그램", "다큐멘터리",
    "음악 프로그램", "오디션 프로그램",
]

def _cats_is_drama(categories: list[str]) -> bool:
    """카테고리 목록에서 드라마 페이지인지 판별."""
    combined = " ".join(categories).lower()
    has_drama = any(kw.lower() in combined for kw in _DRAMA_CAT_KW)
    return has_drama

def _cats_is_variety(categories: list[str]) -> bool:
    """카테고리 목록에서 예능/버라이어티 페이지인지 판별."""
    combined = " ".join(categories).lower()
    return any(kw.lower() in combined for kw in _VARIETY_CAT_KW)

def _cats_is_non_drama(categories: list[str]) -> bool:
    """카테고리 목록에서 비드라마 원작 페이지인지 판별.

    '소설을 바탕으로 한 텔레비전 드라마' 처럼 드라마 전용 키워드가 함께 있으면 False.
    주의: '텔레비전 프로그램'은 애니메이션·예능도 포함하므로 드라마 판별에 쓰지 않음.
    """
    combined = " ".join(categories).lower()
    has_non_drama = any(kw.lower() in combined for kw in _NON_DRAMA_CAT_KW)
    if not has_non_drama:
        return False
    # 비드라마 키워드가 있더라도 드라마 전용 키워드가 있으면 드라마 원작 포함 페이지로 봄
    # ("텔레비전 프로그램", "방송 프로그램"은 제외 — 애니/예능도 포함하므로)
    _DRAMA_SPECIFIC_KW = [
        "텔레비전 드라마", "tv 드라마", "드라마 시리즈", "웹드라마",
        "텔레비전_드라마", "tv_드라마", "시트콤", "드라마화",
    ]
    has_drama_specific = any(kw in combined for kw in _DRAMA_SPECIFIC_KW)
    return not has_drama_specific


async def _wiki_http_get(url: str, client) -> tuple[str, str]:
    """Wikipedia URL을 API로 가져옵니다. (html, final_url) 반환.
    URL에서 페이지 제목을 추출하여 MediaWiki API를 통해 HTML을 수신합니다."""
    import urllib.parse as _up
    # URL에서 페이지 제목 추출
    # https://ko.wikipedia.org/wiki/사랑의_불시착 → 사랑의_불시착
    # https://ko.wikipedia.org/w/index.php?search=사랑의+불시착 → 검색 처리
    if "/wiki/" in url:
        slug = url.split("/wiki/", 1)[1].split("?")[0].split("#")[0]
        page_title = _up.unquote(slug)
    elif "search=" in url:
        # 검색 URL이면 제목으로 변환
        from urllib.parse import parse_qs, urlparse
        qs = parse_qs(urlparse(url).query)
        page_title = qs.get("search", [""])[0] or qs.get("page", [""])[0]
    else:
        page_title = url.rstrip("/").rsplit("/", 1)[-1]
    return await _wiki_api_get_html(page_title, client)


# ── 본문 섹션 텍스트 정리 ─────────────────────────────────────────────────────
_BLOCK_TAGS = ["p", "div", "li", "tr", "td", "th", "dd", "dt", "ul", "ol", "table", "caption"]


def _el_text(el) -> str:
    """요소의 텍스트. 블록 요소·<br> 경계에서만 공백을 넣고, 링크·강조 같은 인라인 태그 사이에는 넣지 않는다.

    get_text(separator=" ") 는 '<a>윤미래</a>의' 를 '윤미래 의' 로, '〈<b>제목</b>〉' 을 '〈 제목 〉' 로 만들었다.
    <style>/<script> 내용은 본문이 아니므로 제외한다."""
    import copy
    c = copy.copy(el)
    for t in c.find_all(["style", "script"]):
        t.decompose()
    for br in c.find_all("br"):
        br.replace_with(" ")
    for t in c.find_all(_BLOCK_TAGS):
        t.insert(0, " ")
        t.append(" ")
    return c.get_text("")


def tidy_text(t: str) -> str:
    """이미 추출된 텍스트의 어색한 공백 정리: '〈 제목 〉 ( 가수 )' → '〈제목〉 (가수)', '김 , 이' → '김, 이'.
    여러 번 적용해도 결과가 같다."""
    if not t:
        return t
    t = re.sub(r"([(\[〈《「『])\s+", r"\1", t)
    t = re.sub(r"\s+([)\]〉》」』,])", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


_ORPHAN_LABELS = {"출처", "참고", "주", "비고", "각주"}


def clean_sections(sections):
    """저장·표시용 본문 섹션 정리 (멱등). 새 리스트를 돌려준다.

    - 표가 있는 섹션의 texts 가 표 내용을 조각조각 중복한 것이면(표 셀 텍스트의 부분 문자열이 80% 이상) 그 조각을 지운다
    - 텍스트·표 셀의 어색한 공백 정리 (tidy_text)
    - '이정효 :' 처럼 콜론 뒤 내용이 비어 있는 항목은 콜론을 떼고, '출처:' 같은 라벨만 남은 항목은 지운다
    """
    def _norm(s):
        return re.sub(r"\s+", "", s or "")

    def _one(sec):
        if not isinstance(sec, dict):
            return sec
        out = dict(sec)
        tables = []
        for tb in sec.get("tables") or []:
            rows = []
            for row in tb.get("rows") or []:
                rows.append([{**c, "text": tidy_text(c.get("text") or "")} if isinstance(c, dict) else c for c in row])
            tables.append({**tb, "rows": rows})
        texts = [t for t in (sec.get("texts") or []) if isinstance(t, str)]
        if tables and len(texts) >= 3:
            flat = _norm("".join(c.get("text", "") for tb in tables for row in tb["rows"] for c in row if isinstance(c, dict)))
            dup = [t for t in texts if _norm(t) and _norm(t) in flat]
            if len(dup) / len(texts) >= 0.8:
                drop = set(dup)
                texts = [t for t in texts if t not in drop]
        cleaned = []
        for t in texts:
            t = tidy_text(t)
            m = re.fullmatch(r"(.+?)\s*[:：]", t)
            if m:
                label = m.group(1).strip()
                if label in _ORPHAN_LABELS:
                    continue
                t = label
            if t:
                cleaned.append(t)
        out["texts"] = cleaned
        out["tables"] = tables
        out["subsections"] = [_one(s) for s in (sec.get("subsections") or [])]
        return out

    return [_one(s) for s in (sections or [])]


async def parse_wiki_page(client, wiki_url: str) -> dict:
    """위키피디아 문서에서 정보박스 + 외부링크 파싱 (httpx 기반, Playwright 불필요)

    [개선된 흐름]
    1. action=query 로 페이지 메타(missing/disambig/categories)를 먼저 확인
    2. missing → 즉시 "문서 없음" 반환 (HTML 로드 없이)
    3. 비드라마 원작(웹툰/만화/소설) 카테고리 → _(드라마) 버전 존재 시 해당 URL 사용
    4. 동음이의어 → 드라마 링크 선택 후 이동
    5. hatnote → 현재 페이지가 이미 드라마 카테고리이면 이동 건너뜀
    """
    import urllib.parse as _up

    # ── URL에서 페이지 제목 추출 ──────────────────────────────────────────────
    if "/wiki/" in wiki_url:
        _slug = wiki_url.split("/wiki/", 1)[1].split("?")[0].split("#")[0]
        _page_title = _up.unquote(_slug)
    else:
        _page_title = wiki_url.rstrip("/").rsplit("/", 1)[-1]

    # ── STEP 1: HTML 직접 로드 (API 불필요) ────────────────────────────────
    page_url = f"https://ko.wikipedia.org/wiki/{_up.quote(_page_title.replace(' ', '_'))}"
    html, wiki_url = await _wiki_fetch_html(page_url)
    if not html:
        return {"url": wiki_url, "error": "위키피디아 페이지를 불러올 수 없습니다."}

    soup = BeautifulSoup(html, "lxml")
    meta = _parse_meta_from_html(soup, wiki_url)
    logging.debug(f"[parse_wiki_page] meta for '{_page_title}': cats={meta['categories'][:3]}")

    # ── STEP 2: 비드라마 원작 → _(드라마) 자동 전환 ──────────────────────────
    if not meta["missing"] and not meta["is_disambiguation"] \
            and _cats_is_non_drama(meta["categories"]):
        _base = re.sub(r"_?\([^)]*\)$", "", _page_title).strip()
        _drama_title = f"{_base}_(드라마)"
        _drama_url = f"https://ko.wikipedia.org/wiki/{_up.quote(_drama_title.replace(' ', '_'))}"
        # 'X (드라마)' 문서가 있는지는 로컬 dumps 인덱스로 먼저 확인한다 (없으면 위키에 404 요청을 보내지 않음)
        if _dump_has_title(f"{_base} (드라마)") is False:
            _drama_html, _drama_final = "", _drama_url
        else:
            _drama_html, _drama_final = await _wiki_fetch_html(_drama_url)
        if _drama_html and "존재하지 않습니다" not in _drama_html:
            logging.info(f"[parse_wiki_page] 비드라마 → _(드라마) 전환: {_drama_title}")
            _page_title = _drama_title
            wiki_url = _drama_final
            html = _drama_html
            soup = BeautifulSoup(html, "lxml")
            meta = _parse_meta_from_html(soup, wiki_url)
        else:
            logging.info(f"[parse_wiki_page] _(드라마) 없음, 원본 유지: {_page_title}")

    # ── STEP 3: 동음이의어 → 드라마 링크 선택 ──────────────────────────────
    if meta["is_disambiguation"]:
        drama_url = _find_drama_link_in_disambiguation(soup)
        if drama_url:
            logging.info(f"[parse_wiki_page] 동음이의어 → 드라마 링크: {drama_url}")
            wiki_url = drama_url
            html, wiki_url = await _wiki_fetch_html(drama_url)
            if html:
                soup = BeautifulSoup(html, "lxml")
                meta = _parse_meta_from_html(soup, wiki_url)
                _page_title = drama_url.split("/wiki/", 1)[-1].split("?")[0]
                _page_title = _up.unquote(_page_title)

    # ── STEP 4: missing 최종 확인 ────────────────────────────────────────────
    if meta["missing"] or "존재하지 않습니다" in html or "noarticletext" in html:
        logging.info(f"[parse_wiki_page] 문서 없음: {_page_title}")
        return {"url": wiki_url, "error": "문서 없음"}

    # ── STEP 6: 현재 페이지가 드라마인지 API 카테고리로 판별 ─────────────────
    # HTML 카테고리 파싱 대신 action=query 결과 활용
    _cur_is_drama = _cats_is_drama(meta["categories"])
    # API 카테고리가 부족할 경우 인포박스로 보강
    if not _cur_is_drama:
        _ib = soup.select_one(".infobox")
        if _ib and any(kw in _ib.get_text() for kw in ["방송 채널", "방송 기간", "방영 채널"]):
            _cur_is_drama = True

    # ── STEP 7: hatnote "다른 뜻" 링크 처리 (비드라마 페이지만) ─────────────
    # 이미 드라마 페이지이면 건너뜀 → "태양의 후예" 같은 오이동 방지
    _WIKI_NS = ["/위키백과:", "/특수:", "/도움말:", "/분류:", "동음이의어_문서"]

    if not _cur_is_drama and not _is_disambiguation(soup):
        hatnote_target = None

        # 1단계: hatnote 직접 링크
        for hatnote in soup.select(".hatnote, .dablink"):
            hatnote_text = hatnote.get_text()
            if not any(kw in hatnote_text for kw in ["드라마", "TV", "시리즈", "텔레비전"]):
                continue
            for a in hatnote.select("a[href]"):
                href = a.get("href", "")
                link_text = a.get_text(strip=True)
                if any(ns in href for ns in _WIKI_NS):
                    continue
                if href.startswith("/wiki/"):
                    target = "https://ko.wikipedia.org" + href
                elif href.startswith("https://ko.wikipedia.org/wiki/"):
                    target = href
                else:
                    continue
                combined = (link_text + " " + target).lower()
                if any(kw in combined for kw in DRAMA_URL_KW):
                    hatnote_target = target
                    break
            if hatnote_target:
                break

        # 2단계: hatnote가 동음이의어 페이지를 거치는 경우
        if not hatnote_target:
            for hatnote in soup.select(".hatnote, .dablink"):
                for a in hatnote.select("a[href]"):
                    href = a.get("href", "")
                    link_text = a.get_text(strip=True)
                    if any(ns in href for ns in _WIKI_NS):
                        continue
                    if "동음이의" in link_text or "동음이의" in href:
                        if href.startswith("/wiki/"):
                            mid_url = "https://ko.wikipedia.org" + href
                        elif href.startswith("https://ko.wikipedia.org/wiki/"):
                            mid_url = href
                        else:
                            continue
                        mid_html, _ = await _wiki_http_get(mid_url, client)
                        mid_soup = BeautifulSoup(mid_html, "lxml")
                        drama_url_via_mid = _find_drama_link_in_disambiguation(mid_soup)
                        if drama_url_via_mid:
                            hatnote_target = drama_url_via_mid
                        break
                if hatnote_target:
                    break

        if hatnote_target:
            html, wiki_url = await _wiki_http_get(hatnote_target, client)
            soup = BeautifulSoup(html, "lxml")

    # ── 비드라마 페이지 감지 → STEP 2에서 action=query 기반으로 이미 처리됨 ──────
    # HTML 기반 재처리: API 카테고리에서 잡지 못한 경우 인포박스 장르로 보완
    page_infobox_genre = ""
    _tmp_box = soup.select_one("table.infobox, table.wikitable")
    if _tmp_box:
        for row in _tmp_box.select("tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if th and td and "장르" in th.get_text():
                page_infobox_genre = td.get_text()

    # 인포박스 장르가 순수 비드라마(웹툰/소설)이고 카테고리에서 못 잡은 경우 보완
    if not _cur_is_drama and page_infobox_genre.strip() in ["웹툰", "만화", "소설", "웹소설"]:
        _base2 = re.sub(r"_?\([^)]*\)$", "", _page_title).strip()
        _drama_title2 = f"{_base2}_(드라마)"
        if not re.search(r"\(.+\)$", _base2):
            _d2_meta = await _wiki_query_meta(_drama_title2, client)
            if not _d2_meta["missing"]:
                logging.info(f"[parse_wiki_page] infobox 장르 기반 _(드라마) 전환: {_drama_title2}")
                html, wiki_url = await _wiki_http_get(
                    f"https://ko.wikipedia.org/wiki/{_up.quote(_drama_title2)}", client)
                soup = BeautifulSoup(html, "lxml")

    # ── 정보박스 ─────────────────────────────────────────────────────────────
    infobox = {}
    wiki_poster = ""
    box = soup.select_one("table.infobox, table.wikitable")
    if box:
        for row in box.select("tr"):
            th = row.select_one("th")
            td = row.select_one("td")
            if th and td:
                key = th.get_text(strip=True)
                # separator="\n"으로 <br>을 개행으로 변환 → 출연진 이름 분리 가능
                val = td.get_text(separator="\n", strip=True)
                # 연속 공백 정리 (단, 개행은 유지)
                val = re.sub(r"[ \t]+", " ", val).strip()
                if key and val:
                    infobox[key] = val

        # ── 인포박스 이미지 (포스터) 추출 ────────────────────────────────────
        # 포스터로 쓰면 안 되는 아이콘/로고/일반/시청등급 이미지 파일명 패턴
        _WIKI_IMG_SKIP = re.compile(
            r"Picto_infobox|Flag_of_|Television_icon|OOjs_UI|Commons-logo"
            r"|Edit-clear|Disambig|Redirect|Question_book|Icon_|Merge-arrow"
            r"|Symbol_|Nuvola_|Crystal_|Gnome-|Gtk-|Tango-|Oxygen-|Silk-"
            r"|Rating_System|Broadcasting.*Rating|TV_Rating|시청가|등급|age.?rating"
            r"|\.svg$|\.svgz$",
            re.IGNORECASE,
        )
        for img in box.select("img[src]"):
            src = img.get("src", "")
            # 위키미디어 이미지만 허용
            # - upload.wikimedia.org: 원본 이미지
            # - thumb.wikimedia.org:  썸네일 (실제 포스터 이미지가 여기서 올 수 있음)
            if "wikimedia.org" not in src:
                continue
            # 아이콘·플래그·로고 계열 이미지 제외 (썸네일 URL 기준 1차 체크)
            if _WIKI_IMG_SKIP.search(src):
                continue
            # protocol-relative → https
            if src.startswith("//"):
                src = "https:" + src
            # thumb.wikimedia.org → upload.wikimedia.org 로 통일 후 원본 URL 추출
            src = src.replace("https://thumb.wikimedia.org/", "https://upload.wikimedia.org/")
            # 썸네일 → 원본 URL 변환
            # 예: //upload.wikimedia.org/wikipedia/ko/thumb/a/ab/Poster.jpg/220px-Poster.jpg
            #  → https://upload.wikimedia.org/wikipedia/ko/a/ab/Poster.jpg
            if "/thumb/" in src:
                # /thumb/hash/hash/파일명/크기-파일명 → /hash/hash/파일명
                parts = src.split("/thumb/", 1)
                base = parts[0]
                rest = parts[1]  # hash/hash/파일명/크기-파일명
                path_parts = rest.split("/")
                if len(path_parts) >= 3:
                    src = base + "/" + "/".join(path_parts[:3])
            # 원본 URL 기준 2차 체크 (SVG 변환 후, URL 인코딩 포함)
            from urllib.parse import unquote as _uq
            if _WIKI_IMG_SKIP.search(_uq(src)):
                continue
            # 작품 로고(…_logo.png) 같은 이미지는 포스터가 아니다. 이걸 포스터로 잡으면
            # 아래 공식사이트 og:image 폴백이 건너뛰어져 결국 포스터가 비게 된다.
            from crawlers.filters import is_logo_like_poster as _is_logo_img
            if _is_logo_img(_uq(src)):
                continue
            wiki_poster = src
            break  # 첫 번째 유효한 이미지만 사용

    # ── 요약 (인포박스 바로 뒤 첫 단락) ─────────────────────────────────────
    summary = ""
    # API 반환 HTML: #mw-content-text 래퍼 없이 바로 .mw-parser-output이 루트
    content_div = (soup.select_one("#mw-content-text .mw-parser-output")
                   or soup.select_one(".mw-parser-output"))
    if content_div:
        # 인포박스(table.infobox) 엘리먼트들 — 이 안의 <p>는 제외
        _infobox_els = set(content_div.select("table.infobox, .wikitable, .infobox"))
        for p in content_div.select("p"):
            # 인포박스 내부이면 건너뜀
            if any(ib in p.parents for ib in _infobox_els):
                continue
            # 이미지 태그 alt 텍스트 제거 (img 요소를 공백으로 대체)
            for img_tag in p.select("img"):
                img_tag.decompose()
            text = re.sub(r"\[\d+\]", "", p.get_text(strip=True))  # 주석 제거
            # raw 위키 마크업 포함 단락 건너뜀 (예: | 외부 링크 = ... }})
            if "| " in text and ("}}" in text or "= " in text):
                continue
            # 파이프 구분자 포함 wiki 이미지 마크업 건너뜀
            # 예: "20px|15세 이상 시청가|link=대한민국의 텔레비전 등급 제도"
            if "|link=" in text or ("|" in text and ("px|" in text or "시청가" in text)):
                continue
            # 날짜·숫자만 있는 짧은 단락이나 방영 정보 패턴은 건너뜀
            if len(text) < 50:
                continue
            # "~년 ~월"로 시작하거나 날짜/숫자 비율이 높으면 건너뜀
            _date_ratio = sum(1 for c in text if c.isdigit() or c in "년월일~·") / max(len(text), 1)
            if _date_ratio > 0.3 and len(text) < 80:
                continue
            summary = text
            break

    # ── 본문이 아닌 요소 제거 ─────────────────────────────────────────────────
    # 문서 하단의 내비게이션 박스(navbox: "v t e  … 드라마 목록"), 사이드바, 분류 링크, 통제 정보 등은
    # 마지막 섹션(예: 등장 인물) 안에 들어 있어서 그대로 두면 수백 줄의 관련 문서 목록이 본문으로 저장된다.
    if content_div:
        for _el in content_div.select(
            ".navbox, .navbox-styles, .vertical-navbox, .sidebar, .catlinks, "
            ".authority-control, .mw-authority-control, .sistersitebox, .printfooter, [role='navigation']"
        ):
            try:
                _el.decompose()
            except Exception:
                pass   # 이미 제거된 부모 안의 요소

    # ── 전체 본문 섹션 파싱 ───────────────────────────────────────────────────
    # 제외할 섹션 (참고용·메타정보)
    _SKIP_SECTIONS = {"각주", "같이 보기", "외부 링크", "동시간대", "참고 사항"}

    def _is_icon_flow_list(lst) -> bool:
        """이미지(계급장·아이콘)와 화살표(→)로 변화 과정을 보여 주는 목록인지.

        예) '박민석 [이병 아이콘] → [일병 아이콘]'. 이미지를 버리면 '박민석 →' 처럼 이름과 화살표만 남아
        의미가 없어지므로 이런 목록은 본문으로 저장하지 않는다."""
        return any(li.find("img") is not None and "→" in li.get_text() for li in lst.select("li"))

    def _clean_text(t: str) -> str:
        """위키피디아 주석([1], [ 2 ] 등)·연속 공백 제거"""
        t = re.sub(r"\[\s*\d+\s*\]", "", t)   # [1] 또는 [ 2 ] 형태 모두 제거
        return tidy_text(t)

    def _parse_table_el(tbl) -> dict:
        """wikitable → 행·열 파싱"""
        caption_el = tbl.select_one("caption")
        caption = caption_el.get_text(strip=True) if caption_el else ""
        rows = []
        for tr in tbl.select("tr"):
            cells = tr.select("th, td")
            if not cells:
                continue
            row = []
            for c in cells:
                colspan = int(c.get("colspan") or 1)
                rowspan = int(c.get("rowspan") or 1)
                row.append({
                    "text": _clean_text(_el_text(c)),
                    "colspan": colspan if colspan > 1 else None,
                    "rowspan": rowspan if rowspan > 1 else None,
                })
            rows.append(row)
        return {"caption": caption, "rows": rows}

    def _parse_section_el(sec_el) -> dict | None:
        """<section> 요소를 재귀 파싱하여 구조화된 딕셔너리 반환"""
        from bs4 import NavigableString, Tag as _Tag

        # 위키피디아 각주 참조(<sup class="reference">) 미리 제거
        for sup in sec_el.find_all("sup", class_="reference"):
            sup.decompose()

        # 첫 헤딩 찾기
        heading = sec_el.find(["h2", "h3", "h4"], recursive=False)
        if not heading:
            # 직계 자식이 아닌 경우 전체에서 찾기
            heading = sec_el.find(["h2", "h3", "h4"])
        if not heading:
            return None

        # 헤딩 텍스트 정리 ("[편집]", "[1]" 등 각주 제거)
        span = heading.select_one("span.mw-headline")
        raw_title = span.get_text(strip=True) if span else heading.get_text(strip=True)
        title = re.sub(r"\[편집\]|\[edit\]", "", raw_title)
        title = re.sub(r"\[\s*\d+\s*\]", "", title).strip()

        # 제외 섹션
        if title in _SKIP_SECTIONS:
            return None

        level = int(heading.name[1])  # h2 → 2

        data = {
            "title": title,
            "level": level,
            "texts": [],
            "tables": [],
            "subsections": [],
        }

        for child in sec_el.children:
            if isinstance(child, NavigableString):
                continue
            if not isinstance(child, _Tag):
                continue
            tag = child.name

            # 하위 section 재귀
            if tag == "section":
                sub = _parse_section_el(child)
                if sub:
                    data["subsections"].append(sub)
                continue

            # 헤딩 자체는 건너뜀
            if tag in ("h2", "h3", "h4"):
                continue

            # div.mw-heading 처리 (신형 위키피디아)
            if tag == "div" and "mw-heading" in child.get("class", []):
                continue

            # 단락
            if tag == "p":
                txt = _clean_text(_el_text(child))
                if txt:
                    data["texts"].append(txt)

            # 목록
            elif tag in ("ul", "ol"):
                if _is_icon_flow_list(child):
                    continue
                for li in child.select("li"):
                    txt = _clean_text(_el_text(li))
                    if txt:
                        data["texts"].append(txt)

            # 정의 목록 (등장인물 설명 등)
            elif tag == "dl":
                for item in child.children:
                    if isinstance(item, NavigableString):
                        continue
                    if item.name in ("dt", "dd"):
                        txt = _clean_text(_el_text(item))
                        if txt:
                            data["texts"].append(txt)

            # 테이블
            elif tag == "table":
                parsed_tbl = _parse_table_el(child)
                if parsed_tbl["rows"]:
                    data["tables"].append(parsed_tbl)

            # figure — 이미지 캡션 추출
            elif tag == "figure":
                caption_el = child.select_one("figcaption")
                if caption_el:
                    txt = _clean_text(_el_text(caption_el))
                    if txt:
                        data["texts"].append(f"[이미지] {txt}")

            # div·blockquote 컨테이너 — 내부 p/ul/li/table 처리
            elif tag in ("div", "blockquote"):
                classes = child.get("class") or []
                # mw-heading 계열은 건너뜀 (이미 헤딩 처리)
                if "mw-heading" in " ".join(classes):
                    continue
                # p 태그
                for p in child.select("p"):
                    if p.find_parent("table"):
                        continue
                    txt = _clean_text(_el_text(p))
                    if txt:
                        data["texts"].append(txt)
                # ul/ol 리스트
                for ul in child.select("ul, ol"):
                    if ul.find_parent("table") or _is_icon_flow_list(ul):
                        continue
                    for li in ul.select("li"):
                        txt = _clean_text(_el_text(li))
                        if txt:
                            data["texts"].append(txt)
                # div-col 등: ul도 p도 없는 경우 텍스트를 줄바꿈 분리
                # (표·스타일 내용은 tables 로 따로 저장되므로 제외)
                if not child.select("p, ul, ol"):
                    import copy as _copy
                    _tmp = _copy.copy(child)
                    for _t in _tmp.select("table, style, script"):
                        _t.decompose()
                    for line in _tmp.get_text(separator="\n").split("\n"):
                        txt = _clean_text(line)
                        if txt and len(txt) > 1:
                            data["texts"].append(txt)
                # 테이블
                for tbl in child.select("table"):
                    parsed_tbl = _parse_table_el(tbl)
                    if parsed_tbl["rows"]:
                        data["tables"].append(parsed_tbl)

        # 제목만 있고 내용(본문·표·하위 섹션)이 하나도 없는 섹션은 저장하지 않는다
        if not (data["texts"] or data["tables"] or data["subsections"]):
            return None
        return data

    # 섹션 파싱 실행
    sections: list[dict] = []
    if content_div:
        # 신형: <section> 태그 기반 파싱
        raw_secs = content_div.find_all("section", recursive=False)
        if raw_secs:
            for sec in raw_secs:
                parsed = _parse_section_el(sec)
                if parsed:
                    sections.append(parsed)
        else:
            # 구형(API 응답): <h2>/<h3> 태그 기반 파싱
            # infobox·table 제거된 원본에서 h2 단위로 섹션 분리
            _SKIP_SECTIONS_FB = {"각주", "같이 보기", "외부 링크", "동시간대", "참고 사항", "외부링크"}
            from bs4 import NavigableString as _NS, Tag as _Tag2
            cur_sec: dict | None = None
            for child in content_div.children:
                if isinstance(child, _NS):
                    continue
                if not isinstance(child, _Tag2):
                    continue
                tag = child.name

                # div.mw-heading 처리 (구형 래퍼)
                if tag == "div" and "mw-heading" in " ".join(child.get("class") or []):
                    hx = child.find(["h2", "h3", "h4"])
                    if hx:
                        child = hx
                        tag = hx.name

                if tag in ("h2", "h3", "h4"):
                    span = child.select_one("span.mw-headline")
                    raw_title = (span.get_text(strip=True) if span
                                 else child.get_text(strip=True))
                    title_txt = re.sub(r"\[편집\]|\[edit\]", "", raw_title).strip()
                    if title_txt in _SKIP_SECTIONS_FB:
                        cur_sec = None
                        continue
                    level = int(tag[1])
                    cur_sec = {"title": title_txt, "level": level,
                               "texts": [], "tables": [], "subsections": []}
                    sections.append(cur_sec)
                    continue

                if cur_sec is None:
                    continue  # 첫 h2 이전 (인트로)는 summary로 커버됨

                if tag == "p":
                    txt = _clean_text(_el_text(child))
                    if txt:
                        cur_sec["texts"].append(txt)
                elif tag in ("ul", "ol"):
                    if _is_icon_flow_list(child):
                        continue
                    for li in child.select("li"):
                        if li.find_parent("table"):
                            continue
                        txt = _clean_text(_el_text(li))
                        if txt:
                            cur_sec["texts"].append(txt)
                elif tag == "dl":
                    for item in child.children:
                        if isinstance(item, _NS):
                            continue
                        if hasattr(item, "name") and item.name in ("dt", "dd"):
                            txt = _clean_text(_el_text(item))
                            if txt:
                                cur_sec["texts"].append(txt)
                elif tag == "table":
                    parsed_tbl = _parse_table_el(child)
                    if parsed_tbl["rows"]:
                        cur_sec["tables"].append(parsed_tbl)
                elif tag == "figure":
                    cap = child.select_one("figcaption")
                    if cap:
                        txt = _clean_text(_el_text(cap))
                        if txt:
                            cur_sec["texts"].append(f"[이미지] {txt}")
                elif tag in ("div", "blockquote"):
                    for p in child.select("p"):
                        if p.find_parent("table"):
                            continue
                        txt = _clean_text(_el_text(p))
                        if txt:
                            cur_sec["texts"].append(txt)
                    for li in child.select("li"):
                        txt = _clean_text(_el_text(li))
                        if txt:
                            cur_sec["texts"].append(txt)
                    for tbl in child.select("table"):
                        parsed_tbl = _parse_table_el(tbl)
                        if parsed_tbl["rows"]:
                            cur_sec["tables"].append(parsed_tbl)

    # infobox_raw: 인포박스 원본 전체 키-값 (정리 전)
    infobox_raw = {k: _clean_infobox_val(v) for k, v in infobox.items()}

    # ── 외부링크 수집 (공식사이트 후보 선별용으로만 사용) ────────────────────────
    # 1순위: "외부 링크" 섹션
    # 2순위: 인포박스 내 "외부 링크"/"공식 사이트"/"웹사이트"/"홈페이지" 행
    # 3순위: 본문 전체에서 알려진 공식 방송사/OTT 도메인 URL만 수집
    #        (뉴스·리뷰 기사 오탐 방지를 위해 도메인 화이트리스트 적용)
    # ※ 각 단계에서 pick_official_site 결과가 None이면 다음 단계로 진행
    official_pick: str | None = None

    # 1) 외부 링크 섹션
    # 위키피디아 HTML 구조는 두 가지 형태가 혼용됨:
    #   [신형] section > div.mw-heading.mw-heading2 > h2#외부_링크
    #          section > ul > li > a.external.text
    #   [구형] h2/span[id=외부_링크] → find_next("ul")
    # → 신형은 a.external 클래스로 바로 찾을 수 있어 더 정확함
    ext_links: list[dict] = []

    ext_heading = (
        soup.find(["h2", "h3", "h4"], id=re.compile(r"외부|External", re.I))
        or soup.find("span", id=re.compile(r"외부|External", re.I))
    )
    if ext_heading:
        # ── 신형: section 컨테이너 안의 a.external 링크 수집 ──────────────────
        section = ext_heading.find_parent("section")
        if section:
            # section 안의 모든 a.external 링크 (class="external text")
            for a in section.select("a.external[href], a[class*='external'][href]"):
                href = a.get("href", "")
                if href.startswith("http"):
                    ext_links.append({"text": a.get_text(strip=True), "url": href})

        # ── 구형 fallback: find_next("ul") ────────────────────────────────────
        if not ext_links:
            ul = ext_heading.find_next("ul")
            if ul:
                for li in ul.select("li"):
                    for a in li.find_all("a", href=True):
                        href = a["href"]
                        if href.startswith("http"):
                            ext_links.append({"text": a.get_text(strip=True), "url": href})

    official_pick = pick_official_site(ext_links)

    # (직접 HTML 스크래핑 방식 — 외부링크는 HTML에서 이미 추출됨)

    # 2) 인포박스 행 중 공식 사이트 관련 필드
    if not official_pick:
        OFFICIAL_IB_KEYS = {"외부 링크", "공식 사이트", "웹사이트", "홈페이지", "official", "website"}
        ib_links: list[dict] = []
        box_ib = soup.select_one("table.infobox, table.wikitable")
        if box_ib:
            for row in box_ib.select("tr"):
                th = row.select_one("th")
                if not th:
                    continue
                th_text = th.get_text(strip=True).lower()
                if any(k in th_text for k in OFFICIAL_IB_KEYS):
                    for a in row.select("a[href]"):
                        href = a.get("href", "")
                        if href.startswith("http") and "wikipedia.org" not in href:
                            ib_links.append({"text": a.get_text(strip=True), "url": href})
        official_pick = pick_official_site(ib_links)

    # 3) 본문 전체 fallback — 방송사·OTT 화이트리스트 도메인만 허용
    #    단, news./press. 서브도메인 및 아카이브·검색 URL은 제외
    if not official_pick:
        OFFICIAL_DOMAINS = [
            "program.kbs.co.kr", "programs.kbs.co.kr",
            "programs.sbs.co.kr", "program.sbs.co.kr",
            "imbc.com", "program.imbc.com",
            "tv.jtbc.co.kr", "programs.jtbc.co.kr", "tv.jtbc.joins.com",
            "tvn.com", "tvngo.com", "program.tving.com",
            "interest.me",  # 구 tvN/OCN 공식 페이지 (program.interest.me/tvn, /ocn)
            "ocn.co.kr", "mbn.co.kr",
            "tvchosun.com", "ichannela.com", "channela.com",
            "enachannel.com", "ena.co.kr", "skylifetv.co.kr",
            "tving.com", "wavve.com", "disneyplus.com",
            "watcha.com", "seezn.com", "laftel.net",
            "tv.naver.com", "tv.kakao.com",
            "page.kakao.com",  # 카카오페이지
        ]
        # 뉴스·아카이브 서브도메인 제외 패턴
        EXCLUDE_SUBDOMAINS = ["news.", "press.", "archive.", "web.archive", "search."]
        body_links: list[dict] = []
        seen_body: set = set()
        _body_root = soup.select_one("#mw-content-text") or soup.select_one(".mw-parser-output") or soup
        for a in _body_root.select("a[href^='http']"):
            href = a["href"]
            if href in seen_body:
                continue
            href_lower = href.lower()
            if any(ex in href_lower for ex in EXCLUDE_SUBDOMAINS):
                continue
            if any(d in href_lower for d in OFFICIAL_DOMAINS):
                seen_body.add(href)
                body_links.append({"text": a.get_text(strip=True), "url": href})
        official_pick = pick_official_site(body_links)

    # 공식사이트만 추려서 external_links에 담음 (최대 1개)
    external_links = [official_pick] if official_pick else []

    # ── 페이지 제목 추출 ─────────────────────────────────────────────────────
    page_title = ""
    _h1 = soup.select_one("#firstHeading, h1.firstHeading, h1")
    if _h1:
        page_title = _h1.get_text(strip=True)

    # ── 최종 드라마 여부 검증 ────────────────────────────────────────────────
    _has_broadcast_infobox = any(
        kw in infobox for kw in ["방송 채널", "방영 채널", "방송사", "방송 기간", "방영 기간",
                                   "채널", "방송 국가", "방영 국가", "출연자", "제작사"]
    )

    # ① 명시적 비드라마 카테고리(만화·애니·게임 등) + 드라마 카테고리 없음 → 즉시 거부
    #    (예: 귀살→귀멸의 칼날(만화), 검색 결과 상위에 애니·만화 문서가 걸리는 경우)
    if _cats_is_non_drama(meta["categories"]) and not _cur_is_drama:
        logging.info(f"[parse_wiki_page] 비드라마 카테고리 → 거부: {wiki_url} cats={meta['categories'][:3]}")
        return {"url": wiki_url, "error": "드라마 문서를 찾을 수 없습니다"}

    # ② 드라마 카테고리도 없고 방송 인포박스도 없고 인포박스 자체도 없으면 거부
    if not _cur_is_drama and not _has_broadcast_infobox and not infobox:
        logging.info(f"[parse_wiki_page] 드라마 페이지 아님 (카테고리·인포박스 모두 없음): {wiki_url}")
        return {"url": wiki_url, "error": "드라마 문서를 찾을 수 없습니다"}

    # ── 포스터 폴백: wiki_poster 없으면 공식사이트 og:image 시도 ─────────────
    if not wiki_poster and external_links:
        _official_url = external_links[0] if isinstance(external_links[0], str) else external_links[0].get("url", "")
        if _official_url:
            try:
                # wiki_resolver 는 client=None 으로 호출하므로 자체 클라이언트를 사용한다
                if client is not None:
                    _og_resp = await client.get(_official_url, timeout=10, follow_redirects=True)
                else:
                    import httpx as _hx_og
                    async with _hx_og.AsyncClient(
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                 "Accept-Language": "ko-KR,ko;q=0.9"},
                        follow_redirects=True, timeout=10,
                    ) as _og_client:
                        _og_resp = await _og_client.get(_official_url)
                if _og_resp.status_code == 200:
                    _og_soup = BeautifulSoup(_og_resp.text, "lxml")
                    _og_img = (
                        _og_soup.find("meta", property="og:image")
                        or _og_soup.find("meta", attrs={"name": "og:image"})
                    )
                    if _og_img:
                        _og_src = _og_img.get("content", "")
                        from crawlers.filters import is_logo_like_poster as _is_logo_og
                        if _og_src and _og_src.startswith("http") and not _is_logo_og(_og_src):
                            wiki_poster = _og_src
                            logging.info(f"[parse_wiki_page] 공식사이트 og:image 포스터: {_og_src[:60]}")
            except Exception as _e:
                logging.debug(f"[parse_wiki_page] 공식사이트 og:image 실패: {_e}")

    _page_cats = meta.get("categories", [])
    return {
        "wiki_url":     wiki_url,
        "title":        page_title,         # 위키피디아 페이지 제목
        "infobox":      infobox,
        "infobox_raw":  infobox_raw,
        "summary":      summary,
        "sections":     clean_sections(sections),   # 전체 본문 섹션 구조
        "external_links": external_links,  # 공식사이트 링크 0~1개
        "wiki_poster":  wiki_poster,        # 인포박스 포스터 or 공식사이트 og:image
        "categories":   _page_cats,         # 위키피디아 카테고리 목록
        "is_drama":     _cats_is_drama(_page_cats),
        "is_variety":   _cats_is_variety(_page_cats),
    }
