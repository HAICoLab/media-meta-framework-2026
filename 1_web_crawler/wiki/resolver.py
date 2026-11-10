"""
위키피디아 문서 선택(Resolver) — 모든 수집 경로가 공유하는 단 하나의 로직

사용처 (전부 이 모듈을 호출 → 같은 제목이면 항상 같은 문서/같은 본문):
  - 수동검색         routers/library.py → crawlers.metadata.collect_drama_metadata_with_context
  - CJ ENM 자동수집  services/bwc_sync.py
  - com2usN 자동수집 crawlers.com2usn

선택 규칙 (Dumps 기준, 엄격):
  1. opensearch 로 후보 제목을 얻고, 검색어와 전혀 무관한 후보는 버린다.
  2. 후보 문서를 가져온다 (동음이의어 → 드라마 링크 이동 등은 parse_wiki_page 가 처리).
  3. **최종 도착한 문서의 제목**을 wiki_dump_index.db 와 대조한다.
       - Dumps 에 드라마/예능 분류로 있으면 채택
       - 비드라마(영화·만화·라디오 등)이거나 Dumps 에 없으면 버림
     (Dumps 인덱스가 없을 때만 예전 방식인 '인포박스 존재'로 폴백)
  4. content_type(drama/variety)이 주어지면 반대 종류 문서는 버린다.
  5. 채택할 문서가 없으면 {"no_wiki": True, ...} — "위키피디아 문서가 없습니다".
"""

import json
import logging
import re
import sqlite3

from core.paths import WIKI_INDEX as _INDEX_DB

logger = logging.getLogger(__name__)

NO_WIKI_TEXT = "위키피디아 문서가 없습니다"

_WIKI_API = "https://ko.wikipedia.org/w/api.php"

_DRAMA_KW = ["텔레비전 드라마", "TV 드라마", "드라마 시리즈", "웹드라마", "텔레비전 시리즈",
             "시트콤", "드라마화"]
_VARIETY_KW = ["예능", "버라이어티", "토크쇼", "오락 프로그램", "리얼리티", "관찰 예능",
               "쿠킹 프로그램", "교양 프로그램", "음악 프로그램", "오디션 프로그램"]
_NON_DRAMA_KW = [
    "만화", "애니메이션", "웹툰", "소설", "웹소설", "영화", "게임", "음반",
    "라디오 프로그램", "음악 그룹", "밴드", "가수", "뮤지션", "싱글", "앨범",
    "결성된 음악", "해체된 음악", "록 밴드", "보이밴드", "걸그룹", "아이돌 그룹",
    "비디오 게임", "스포츠 선수", "운동선수",
]

# 본문에서 제외할 섹션 (각주·링크류)
_SKIP_SECTIONS = {"각주", "같이 보기", "외부 링크", "동시간대", "참고 사항", "외부링크"}


def dump_index_available() -> bool:
    return _INDEX_DB.exists()


def dump_classify(title: str) -> str:
    """Dumps 인덱스에서 제목의 분류 판별.

    반환: 'drama' | 'variety' | 'non_drama' | 'unknown'(분류 불명) | 'missing'(인덱스에 없음)
    """
    if not _INDEX_DB.exists():
        return "missing"
    try:
        con = sqlite3.connect(str(_INDEX_DB))
        try:
            row = con.execute(
                "SELECT categories_json FROM wiki_pages WHERE title=?", (title,)
            ).fetchone()
        finally:
            con.close()
    except Exception as e:
        logger.debug(f"[wiki_resolver] dump 조회 실패 ({title}): {e}")
        return "missing"
    if not row:
        return "missing"

    cats = " ".join(json.loads(row[0] or "[]")).lower()
    is_drama = any(k.lower() in cats for k in _DRAMA_KW)
    is_variety = any(k.lower() in cats for k in _VARIETY_KW)
    if is_variety and not is_drama:
        return "variety"
    if is_drama:
        return "drama"
    if any(k.lower() in cats for k in _NON_DRAMA_KW):
        return "non_drama"
    return "unknown"


_PERSON_INFOBOX_KEYS = ("출생", "출생지", "직업", "성별", "국적", "본명", "학력", "신체", "데뷔")
_PROGRAM_INFOBOX_KEYS = ("방송 기간", "방영 기간", "방송 횟수", "편수", "방송 채널", "방영 채널",
                         "연출", "극본", "각본", "출연자", "제작사", "제작진")
_PERSON_CAT_RE = re.compile(r"살아있는 사람|\d{4}년 출생|\d{4}년 사망|의 (?:방송인|아나운서|배우|가수|개그맨|코미디언|모델)")


def _is_person_page(w: dict) -> bool:
    """인물 문서 여부. 인물 분류/인포박스가 있고, 작품 인포박스·드라마 분류가 없을 때만 True."""
    from wiki.parse import _cats_is_drama, _cats_is_variety

    cats = w.get("categories", []) or []
    infobox = w.get("infobox", {}) or {}
    if any(k in infobox for k in _PROGRAM_INFOBOX_KEYS):
        return False
    if _cats_is_drama(cats) or _cats_is_variety(cats):
        return False
    person_cat = bool(_PERSON_CAT_RE.search(" ".join(cats)))
    person_box = any(k in infobox for k in _PERSON_INFOBOX_KEYS)
    return person_cat or person_box


# 위키 인포박스 표기 → 티빙 장르명('중국드라마')에서 뽑은 국가 표기
_COUNTRY_ALIAS = {"중화인민공화국": "중국", "중화민국": "대만", "미합중국": "미국", "대한민국": "한국"}


def _norm_country(s: str) -> str:
    for k, v in _COUNTRY_ALIAS.items():
        s = s.replace(k, v)
    return s


def _dump_titles_by_name(name: str) -> list[str]:
    """덤프에서 인포박스 '방송명'이 검색어와 같은 문서 제목들.

    외국 드라마는 문서 제목이 원제 발음 표기(예: '재폭설시분')이고 한국 방영명('우리가 만난 겨울')은
    '방송명'에만 있어 제목 검색(opensearch)으로 찾을 수 없다.
    """
    if not name or not _INDEX_DB.exists():
        return []
    key = re.sub(r"\s+", "", name)
    try:
        con = sqlite3.connect(f"file:{_INDEX_DB}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT title, infobox_json FROM wiki_pages WHERE infobox_json LIKE ? LIMIT 50",
                (f'%"방송명": "%{name.replace(" ", "%")}%',),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    out = []
    for t, ib in rows:
        try:
            v = (json.loads(ib or "{}") or {}).get("방송명", "")
        except Exception:
            continue
        if re.sub(r"\s+", "", str(v)) == key:
            out.append(t)
    return out[:5]


def _wiki_country(w: dict) -> str:
    """문서의 제작/방송 국가. 인포박스 우선, 없으면 '○○의 텔레비전 …' 분류에서 추출. 모르면 ''."""
    ib = w.get("infobox", {}) or {}
    for k in ("방송 국가", "방영 국가", "제작 국가", "국가"):
        if ib.get(k):
            return str(ib[k]).strip()
    # 'SBS의 텔레비전 프로그램' 같은 방송사 분류를 국가로 오인하지 않도록 연도 접두가 붙은 분류만 쓴다
    for c in w.get("categories", []) or []:
        m = re.match(r"^\d{4}(?:년 시작한|년대) (\S+?)의 텔레비전", c)
        if m:
            return m.group(1)
    return ""


def _wiki_start_year(w: dict) -> int | None:
    """문서의 방송 시작 연도.

    인포박스 '방송 기간'은 시리즈물에서 최신 시즌 기준이라(예: 일본 '닥터X' = 2021년~) 쓰지 않고
    '○○○○년 시작한 …' 분류 → '○○○○년 텔레비전 드라마' 분류 중 가장 이른 해 → 인포박스 순으로 본다.
    """
    cats = w.get("categories", []) or []
    for c in cats:
        m = re.match(r"^(\d{4})년 시작한", c)
        if m:
            return int(m.group(1))
    years = [int(m.group(1)) for c in cats if (m := re.match(r"^(\d{4})년 텔레비전 드라마", c))]
    if years:
        return min(years)
    ib = w.get("infobox", {}) or {}
    for k in ("방송 기간", "방영 기간"):
        m = re.search(r"(\d{4})년", str(ib.get(k) or ""))
        if m:
            return int(m.group(1))
    return None


def _hint_conflict(w: dict, hint: dict | None) -> str:
    """수집처(TVING 등)가 준 힌트와 위키 문서가 모순되면 사유 문자열, 아니면 ''.

    hint: {"foreign": bool, "country": "일본"|…, "year": 방영 연도(int)}
      - 해외 작품 힌트인데 문서가 한국 작품이거나, 국가가 다르면 모순
      - 작품이 방영된 연도보다 2년 이상 뒤에 시작한 시리즈는 그 작품의 원본일 수 없다(리메이크 등)
    """
    if not hint:
        return ""
    # 수동검색의 '국내/해외' 선택: 문서의 제작 국가가 확실히(카테고리·인포박스) 반대편이면 같은 제목의 다른 작품이다
    want_origin = hint.get("origin")
    if want_origin in ("domestic", "foreign"):
        from services.origin import detect_origin
        o = detect_origin({"wikipedia": {"categories": w.get("categories"), "infobox": w.get("infobox")}})
        if o["group"] in ("domestic", "foreign") and o["group"] != want_origin and o["confidence"] in ("high", "medium"):
            return f"국가 불일치(요청={'국내' if want_origin == 'domestic' else '해외'}, 문서={o['country']})"
    country = _norm_country(_wiki_country(w))
    if hint.get("foreign") and country:
        want = _norm_country((hint.get("country") or "").strip())
        if want:
            if want not in country:
                return f"국가 불일치(힌트={want}, 문서={country})"
        elif "한국" in country:
            return f"해외작품 힌트인데 문서는 한국 작품({country})"
    year, start = hint.get("year"), _wiki_start_year(w)
    if year and start and year < start - 1:
        return f"방영연도 불일치(힌트={year}, 문서 시작={start})"
    return ""


_CHANNEL_KEYS = ("방송 채널", "추가 채널", "추가 편성", "채널", "방송사", "제작", "제작사", "기획")


def _channel_match(w: dict, hint: dict | None) -> bool | None:
    """수집처 채널(방송사)이 문서의 방송 채널/제작사 항목에 나타나는지.

    True/False, 비교 불가(힌트 없음·해외작품·문서에 항목 없음)면 None.
    해외작품은 티빙 채널이 재배급처(iMBC·엠디에스스피어 등)라 원방송사와 달라 비교하지 않는다.
    하드 필터가 아니라 '일치하는 후보 우선' 용도로만 쓴다(OTT·재배급 작품은 일치하지 않는 경우가 많다).
    """
    channel = ((hint or {}).get("channel") or "").strip()
    if not channel or (hint or {}).get("foreign"):
        return None
    ib = w.get("infobox", {}) or {}
    blob = " ".join(str(ib.get(k, "")) for k in _CHANNEL_KEYS)

    def norm(s: str) -> str:
        return re.sub(r"[\s\W_]+", "", s).lower()

    nb = norm(blob)
    if not nb:
        return None
    parts = [p for p in re.split(r"[,/·]", channel) if norm(p)] or [channel]
    return any(norm(p) in nb for p in parts)


def _has_title_overlap(query: str, candidate: str) -> bool:
    """검색어와 후보 제목이 최소한의 연관성(2-gram Jaccard ≥ 0.15)을 가지는지.

    부제가 붙은 검색어('신병4 : 사보타주')는 위키 문서 제목에 부제가 없는 경우가 많아,
    전체 검색어뿐 아니라 부제를 뗀 본제목('신병4')으로도 비교한다."""
    if _title_overlap(query, candidate):
        return True
    main = _SUBTITLE_SPLIT.split((query or "").strip(), maxsplit=1)[0].strip()
    return bool(main) and main != (query or "").strip() and _title_overlap(main, candidate)


def _title_overlap(query: str, candidate: str) -> bool:
    q = re.sub(r"[\s\(\)\[\]_\-·]", "", query.lower())
    c = re.sub(r"[\s\(\)\[\]_\-·]", "", candidate.lower())
    if len(q) <= 1 or len(c) <= 1:
        return True
    if q in c or c in q:
        return True
    q_grams = {q[i:i + 2] for i in range(len(q) - 1)}
    c_grams = {c[i:i + 2] for i in range(len(c) - 1)}
    if not q_grams or not c_grams:
        return False
    union = len(q_grams | c_grams)
    return union > 0 and len(q_grams & c_grams) / union >= 0.15


# ── Dumps 기반 후보 검색 / 문서 로더 (위키 API 호출을 줄이기 위함) ─────────────────────────
_TITLE_INDEX: dict = {"mtime": None, "rows": []}


def _norm_key(s: str) -> str:
    """공백·기호를 무시한 비교 키 ('신병 시즌 2' == '신병시즌2')."""
    return re.sub(r"[\W_]+", "", s or "").lower()


def _dump_title_index() -> list[tuple[str, str, str, bool]]:
    """덤프 문서 제목 목록 [(제목, 전체키, 괄호 앞 키, 괄호 있음)]. 인덱스 파일이 바뀌면 다시 읽는다."""
    if not _INDEX_DB.exists():
        return []
    mtime = _INDEX_DB.stat().st_mtime
    if _TITLE_INDEX["mtime"] != mtime:
        rows = []
        con = sqlite3.connect(f"file:{_INDEX_DB}?mode=ro", uri=True)
        try:
            for (t,) in con.execute("SELECT title FROM wiki_pages"):
                base, paren = (t.split(" (", 1) + [""])[:2] if " (" in t else (t, "")
                rows.append((t, _norm_key(t), _norm_key(base), bool(paren)))
        finally:
            con.close()
        _TITLE_INDEX.update(mtime=mtime, rows=rows)
    return _TITLE_INDEX["rows"]


def _dump_search_titles(q: str, limit: int = 5) -> list[str]:
    """덤프에서 검색어와 '확실히 같은 작품'의 문서 제목들.

    신뢰도 높은 두 경우만 쓴다: ① 제목 완전 일치 ② 'X (드라마)'처럼 괄호 부가설명만 다른 제목.
    접두 일치 같은 느슨한 검색은 덤프에 없는 리다이렉트 때문에 엉뚱한 문서를 고를 수 있어 쓰지 않는다
    (그 경우는 호출하는 쪽이 위키 API 검색으로 넘어간다).
    """
    key = _norm_key(q)
    if not key:
        return []
    rows = _dump_title_index()
    exact = [t for t, k, _b, _p in rows if k == key]
    disamb = sorted(t for t, _k, b, p in rows if p and b == key)
    return list(dict.fromkeys(exact + disamb))[:limit]


def _clean_dump_summary(s: str | None) -> str:
    """덤프 요약이 인포박스/템플릿 찌꺼기('| 외부 링크 = ...', '{{llang|..}}')면 버린다 (라이브 파싱 실패 시 설명 오염 방지)."""
    s = (s or "").strip()
    if s.startswith("|") or "{{" in s or "[[" in s or "<ref" in s:
        return ""
    return s


def _dump_page(title: str) -> dict | None:
    """덤프의 문서 정보를 parse_wiki_page 결과와 같은 모양으로 (본문 섹션은 덤프에 없어 비어 있음)."""
    import urllib.parse as _up
    if not _INDEX_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{_INDEX_DB}?mode=ro", uri=True)
        try:
            r = con.execute(
                "SELECT title, infobox_json, external_links_json, categories_json, wiki_poster_url, summary "
                "FROM wiki_pages WHERE title=?", (title,)).fetchone()
        finally:
            con.close()
    except Exception as e:
        logger.debug(f"[wiki_resolver] dump 문서 조회 실패 ({title}): {e}")
        return None
    if not r:
        return None
    return {
        "title": r[0],
        "wiki_url": "https://ko.wikipedia.org/wiki/" + _up.quote(r[0].replace(" ", "_"), safe="()"),
        "infobox": json.loads(r[1] or "{}"),
        "external_links": json.loads(r[2] or "[]"),
        "categories": json.loads(r[3] or "[]"),
        "wiki_poster": r[4] or "",
        "summary": _clean_dump_summary(r[5]),
        "sections": [],
        "_from_dump": True,
    }


async def _search_titles(title: str) -> list[str] | None:
    """opensearch 로 후보 제목 목록. 검색 결과 없음 → [], 요청 실패 → None."""
    import urllib.parse as _up
    from wiki.parse import _wiki_get_json

    url = (f"{_WIKI_API}?action=opensearch&search={_up.quote(title)}"
           f"&limit=5&format=json&utf8=1")
    d = await _wiki_get_json(url)
    if not d or not isinstance(d, list) or len(d) < 2:
        logger.warning(f"[wiki_resolver] opensearch 실패 ({title})")
        return None
    return d[1]


_SEASON_IN_TITLE = re.compile(r"시즌\s*(\d+)")
_SUBTITLE_SPLIT = re.compile(r"\s*[:：]\s*|\s+[-–—]\s+")
_SEASON_QUERY = re.compile(r"^(.*?[^\d\s])\s*(?:시즌\s*)?(\d{1,2})$")


def _season_of(text: str) -> int | None:
    """문서/후보 제목에 포함된 '시즌 N' 번호 (없으면 None)."""
    m = _SEASON_IN_TITLE.search(text or "")
    return int(m.group(1)) if m else None


def _split_season_query(query: str) -> tuple[str, int] | None:
    """'신병2', '킬러들의 쇼핑몰 2', '○○ 시즌2' → (기본제목, 시즌번호). 4자리 연도('학교 2021')는 제외."""
    q = query.strip()
    # 부제가 붙은 제목('신병4 : 사보타주', '○○ 2 - 부제')은 부제를 떼고 시즌을 판별한다
    q = _SUBTITLE_SPLIT.split(q, maxsplit=1)[0].strip()
    m = _SEASON_QUERY.match(q)
    if not m:
        return None
    base, n = m.group(1).strip(), int(m.group(2))
    return (base, n) if base and n >= 1 else None


def filter_sections(sections: list | None) -> list:
    return [s for s in (sections or []) if s.get("title") not in _SKIP_SECTIONS]


_ALIAS_PAREN = re.compile(r"^(.*?)\s*[(（]([^()（）]+)[)）]\s*$")


def _title_variants(title: str) -> list[str]:
    """'라이브(Live)', 'WATCHER (왓쳐)' 처럼 제목 뒤에 별칭 괄호가 붙은 경우의 대체 검색어.

    원래 제목으로 못 찾았을 때만 쓴다. 한글이 든 쪽(문서 제목은 대개 한글)을 먼저 시도한다."""
    m = _ALIAS_PAREN.match(title or "")
    if not m:
        return []
    outer, inner = m.group(1).strip(), m.group(2).strip()
    if not outer or len(inner) < 2 or inner in ("드라마", "예능", "시즌", "리메이크"):
        return []
    has_ko = lambda t: bool(re.search(r"[가-힣]", t))
    cands = [outer, inner] if has_ko(outer) or not has_ko(inner) else [inner, outer]
    return [c for c in dict.fromkeys(cands) if c != title and len(c) >= 2]


async def resolve_wiki_page(title: str, content_type: str = "", hint: dict | None = None) -> dict:
    """제목 → 위키피디아 문서. 원래 제목으로 문서를 못 찾으면 별칭 괄호를 뗀 제목('라이브(Live)'→'라이브')으로 다시 찾는다."""
    first = await _resolve_wiki_page_once(title, content_type, hint)
    if first and not first.get("no_wiki"):
        return first
    if first == {}:                      # 검색 자체 실패(네트워크 등)는 그대로 돌려준다
        return first
    for alt in _title_variants((title or "").strip()):
        w = await _resolve_wiki_page_once(alt, content_type, hint)
        if w and not w.get("no_wiki"):
            logger.info(f"[wiki_resolver] '{title}' → 별칭 제거 검색어 '{alt}' 로 문서 찾음")
            return w
    return first


async def _resolve_wiki_page_once(title: str, content_type: str = "", hint: dict | None = None) -> dict:
    """제목 → 위키피디아 문서(parse_wiki_page 결과 dict).

    반환:
      - 문서 있음: parse_wiki_page 결과 (sections 는 filter_sections 적용, `_dump_class` 추가)
      - 문서 없음: {"no_wiki": True, "summary": NO_WIKI_TEXT, "error": NO_WIKI_TEXT, "wiki_url": ""}
      - 검색 자체 실패(네트워크 등): {}
    """
    from wiki.parse import parse_wiki_page, _cats_is_drama, _cats_is_variety

    title = (title or "").strip()
    if not title:
        return {}

    try:
        season_q = _split_season_query(title)
        want_season = season_q[1] if season_q else None
        ct = (content_type or "").lower()
        want_variety = ct in ("variety", "예능")
        want_drama = ct in ("drama", "드라마")
        use_dump = dump_index_available()

        tried_final: set[str] = set()
        trusted: set[str] = set()
        _deferred = None   # 채널 불일치로 보류한 첫 후보 (w, kind, final_title)
        # 국내 예능·교양은 방송사가 다른 같은 제목 프로그램(tvN '끝까지 간다' ↔ JTBC '백인백곡 끝까지 간다')이
        # 많고 재배급·OTT 사례가 거의 없으므로, 채널이 다른 후보는 보류분으로도 쓰지 않는다(→ 문서 없음).
        _strict_channel = want_variety or ct in ("culture", "교양")

        def _order_by_season(cands: list[str], extra: list[str]) -> list[str]:
            """시즌 번호가 있는 검색어: 해당 시즌 문서 우선, 시즌 표기 없는 문서 뒤, 다른 시즌 문서는 제외."""
            pool = list(dict.fromkeys(extra + cands))
            exact = [c for c in pool if _season_of(c) == want_season]
            unmarked = [c for c in cands + extra if _season_of(c) is None]
            return list(dict.fromkeys(exact + unmarked))

        async def _finish(w: dict, kind: str) -> dict:
            """채택한 문서를 돌려준다. 덤프에서 읽은 문서는 본문 섹션만 라이브로 1회 보강한다."""
            if w.pop("_from_dump", False):
                live = await parse_wiki_page(None, w["wiki_url"])
                if live and not live.get("error") and (live.get("title") or "").strip() == w["title"]:
                    w = live
                else:
                    logger.info(f"[wiki_resolver] '{w['title']}' 본문 섹션 보강 실패 → 덤프 정보만 사용")
            w["sections"] = filter_sections(w.get("sections"))
            w["_dump_class"] = kind
            from core.database import canon_wiki_url   # 지연 import (순환 import 방지)
            w["wiki_url"] = canon_wiki_url(w.get("wiki_url"))   # 라이브/덤프 경로마다 달랐던 URL 표기를 통일
            return w

        async def _evaluate(cands: list[str]) -> dict | None:
            """후보를 차례로 검사해 첫 적합 문서를 반환(없으면 None). 채널 불일치 후보는 _deferred 에 보류."""
            nonlocal _deferred
            for cand in cands:
                if cand not in trusted and not _has_title_overlap(title, cand):
                    logger.info(f"[wiki_resolver] '{title}' 후보 '{cand}' → 제목 무관 → 스킵")
                    continue

                # 덤프에 있는 문서는 덤프에서 읽는다(위키 호출 없음). 없으면 라이브로 가져온다.
                w = _dump_page(cand) or await parse_wiki_page(
                    None, f"https://ko.wikipedia.org/wiki/{cand.replace(' ', '_')}"
                )
                if not w or w.get("error"):
                    continue

                final_title = (w.get("title") or cand).strip()
                if final_title in tried_final:
                    continue
                tried_final.add(final_title)

                # 후보 제목이 아니라 '최종 도착 문서'도 검색어와 연관돼야 한다.
                # parse_wiki_page 가 hatnote/동음이의어 링크로 엉뚱한 문서(예: 오아시스 →
                # 'KBS 2TV 월화 드라마' 편성표)로 이동한 경우를 걸러낸다.
                if want_season and _season_of(final_title) not in (None, want_season):
                    logger.info(f"[wiki_resolver] '{title}' → '{final_title}' 다른 시즌 문서 → 스킵")
                    continue
                if cand not in trusted and final_title != cand and not _has_title_overlap(title, final_title):
                    logger.info(f"[wiki_resolver] '{title}' 후보 '{cand}' → 최종 '{final_title}' 제목 무관 → 스킵")
                    continue

                # 인물 문서(방송인·아나운서·배우 등)는 인포박스에 '방송사'가 있어도 작품이 아니다
                if _is_person_page(w):
                    logger.info(f"[wiki_resolver] '{title}' → '{final_title}' 인물 문서 → 스킵")
                    continue

                # 수집처 힌트(해외작품 여부·국가·방영연도)와 모순되는 문서는 같은 제목의 다른 작품이다
                _conflict = _hint_conflict(w, hint)
                if _conflict:
                    logger.info(f"[wiki_resolver] '{title}' → '{final_title}' 힌트 모순({_conflict}) → 스킵")
                    continue

                # ── Dumps 로 최종 문서 검증 ────────────────────────────────
                if use_dump:
                    kind = dump_classify(final_title)
                    # 인덱스에 있다 = 드라마/방송 문서로 선별된 것. 'unknown' 은 방송 문서이나
                    # 분류 키워드가 애매한 경우라 허용. 비드라마이거나 인덱스에 없으면 채택 안 함.
                    if kind in ("non_drama", "missing"):
                        logger.info(f"[wiki_resolver] '{title}' → '{final_title}' dumps={kind} → 스킵")
                        continue
                else:
                    kind = ("variety" if _cats_is_variety(w.get("categories", [])) and
                            not _cats_is_drama(w.get("categories", [])) else "drama")
                    if not w.get("infobox"):
                        continue

                # ── 요청한 종류와 맞는지 ───────────────────────────────────
                if want_variety and kind == "drama" and not _cats_is_variety(w.get("categories", [])):
                    logger.info(f"[wiki_resolver] '{title}' → '{final_title}' 드라마 문서, 예능 요청 → 스킵")
                    continue
                if want_drama and kind == "variety":
                    logger.info(f"[wiki_resolver] '{title}' → '{final_title}' 예능 문서, 드라마 요청 → 스킵")
                    continue

                # 채널(방송사)이 수집처와 다르면 일단 보류하고 다른 후보를 더 본다. 끝까지 일치하는 후보가
                # 없으면 보류해 둔 첫 후보를 쓴다(재배급·OTT 작품은 채널이 달라도 정답인 경우가 많다).
                if _channel_match(w, hint) is False:
                    if _deferred is None:
                        _deferred = (w, kind, final_title)
                    continue

                logger.info(f"[wiki_resolver] '{title}' → 위키 '{final_title}' (dumps={kind})")
                return await _finish(w, kind)
            return None

        # 제목 검색에 안 잡히는 외국 드라마: 덤프에서 '방송명'이 검색어와 같은 문서를 후보에 덧붙인다.
        # 방송명이 정확히 일치하므로 제목 겹침 검사는 면제(문서 제목이 발음 표기라 겹치지 않는다).
        name_cands = _dump_titles_by_name(title)

        # ── 1단계: 덤프 검색 (위키 API 호출 없음) ────────────────────────────────
        dump_cands = _dump_search_titles(title)
        if season_q:
            dump_cands = _order_by_season(
                dump_cands, _dump_search_titles(f"{season_q[0]} 시즌{want_season}") + _dump_search_titles(season_q[0]))
        stage1 = list(dict.fromkeys(dump_cands[:5] + name_cands))
        trusted.update(t for t in name_cands if t not in dump_cands)
        if stage1:
            w = await _evaluate(stage1)
            if w is not None:
                return w
            if _deferred is not None and not _strict_channel:      # 덤프의 확실한 후보가 채널만 달랐다 → API 검색 없이 그 후보 사용
                w, kind, final_title = _deferred
                logger.info(f"[wiki_resolver] '{title}' → 위키 '{final_title}' (dumps={kind}, 채널 불일치 보류분 사용)")
                return await _finish(w, kind)

        # ── 2단계: 위키 API 검색 (덤프로 확실히 못 찾은 경우) ───────────────────────
        candidates = await _search_titles(title)
        if candidates is None:      # 네트워크 등 실패 → '문서 없음'으로 단정하지 않음
            return {}
        if season_q:
            extra = await _search_titles(f"{season_q[0]} 시즌{want_season}") or []
            candidates = _order_by_season(candidates, extra)
        w = await _evaluate([c for c in candidates[:5] if c not in stage1])
        if w is not None:
            return w

        if _deferred is not None and not _strict_channel:
            w, kind, final_title = _deferred
            logger.info(f"[wiki_resolver] '{title}' → 위키 '{final_title}' (dumps={kind}, 채널 불일치 보류분 사용)")
            return await _finish(w, kind)

        logger.info(f"[wiki_resolver] '{title}' → 위키피디아 문서 없음")
        return {"no_wiki": True, "summary": NO_WIKI_TEXT, "error": NO_WIKI_TEXT, "wiki_url": ""}

    except Exception as e:
        logger.warning(f"[wiki_resolver] 위키피디아 조회 실패 ({title}): {e}")
        return {}


def normalize_wiki(w: dict) -> dict:
    """resolve_wiki_page 결과 → 라이브러리 저장용 편의 필드 포함 dict."""
    infobox = w.get("infobox", {}) or {}

    def _split(val: str) -> list:
        val = re.sub(r"\s*\([^)]*\)", "", val or "")
        return [p.strip() for p in re.split(r"[,，、\n]+", val) if p.strip()]

    return {
        "wiki_url":       w.get("wiki_url", ""),
        "wiki_poster":    w.get("wiki_poster", ""),
        "summary":        w.get("summary", ""),
        "infobox":        infobox,
        "sections":       w.get("sections") or [],
        "external_links": w.get("external_links", []),
        "genre":          infobox.get("장르", ""),
        "channel":        infobox.get("방송 채널") or infobox.get("채널") or "",
        "air_date":       infobox.get("방송 기간") or infobox.get("방영 기간") or "",
        "episodes":       infobox.get("방송 횟수") or infobox.get("편수") or "",
        "director":       _split(infobox.get("연출", "") or infobox.get("감독", "")),
        "writer":         _split(infobox.get("극본", "") or infobox.get("각본", "")),
        "cast":           _split(infobox.get("출연자", "") or infobox.get("출연", "")),
    }


async def enrich_with_wikipedia(drama_title: str, content_type: str = "", hint: dict | None = None) -> dict:
    """자동수집(BWC 등)용 래퍼: 문서 → 저장용 dict / no_wiki dict / 실패 시 {}."""
    w = await resolve_wiki_page(drama_title, content_type, hint)
    if not w or w.get("no_wiki"):
        return {"no_wiki": True, "summary": NO_WIKI_TEXT} if w else {}
    return normalize_wiki(w)
