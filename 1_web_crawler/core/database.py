"""
SQLite 데이터베이스 레이어 (aiosqlite)
"""

import aiosqlite
import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .paths import DB_PATH

# ── 로고/배너 URL 필터 (poster 저장·조회 시 공통 사용) ─────────────────────────
_LOGO_RE = re.compile(
    r"systemmng|sitemng|gnbmng|tvN통합브랜딩|sns[공_]유[_]?배너|sns배너"
    r"|브랜딩_sns|logo\.(png|jpg|gif|svg|webp)|/logo/"
    r"|no[_-]?image|default[_-]?image|placeholder",
    re.IGNORECASE,
)
def _is_logo_url(url: str) -> bool:
    """True이면 로고/배너 URL → poster로 사용 불가"""
    if not url:
        return False
    # YouTube 썸네일은 유효한 포스터
    if "img.youtube.com/vi/" in url:
        return False
    # CJ ENM 포토그룹은 유효한 포스터
    if "/pcms/photogroup/" in url or "/pcms/character/" in url:
        return False
    # com2usN API 이미지(api.com2usn.com/files/...)는 실제로 200 OK로 접근 가능한 포스터
    if "api.com2usn.com/files/" in url:
        return False
    return bool(_LOGO_RE.search(url))

def _clean_poster(url: str) -> str:
    """로고 URL이면 빈 문자열 반환, 아니면 원본 반환"""
    return "" if _is_logo_url(url) else url


# (정규식, 통일 표기) — 위에서부터 처음 일치하는 항목을 사용한다. 새 채널 계열은 여기에만 추가하면 된다.
_CHANNEL_ALIASES = [
    (r"tvn(?![a-z])",              "tvN"),        # tvN, tvN STORY, tvN SHOW, tvN DRAMA
    (r"kbs(?![a-z])",              "KBS"),        # KBS, KBS 1TV, KBS 2TV, KBS2, KBS Joy
    (r"mbc(?![a-z])",              "MBC"),        # MBC, MBC TV, MBC every1
    (r"sbs(?![a-z])",              "SBS"),        # SBS, SBS TV, SBS Plus
    (r"jtbc(?![a-z])",             "JTBC"),
    (r"ena(?![a-z])",              "ENA"),
    (r"ebs(?![a-z])",              "EBS"),        # EBS, EBS1, EBS2
    (r"(채널\s*a|channel\s*a)(?![a-z])", "채널A"),
    (r"(넷플릭스|netflix)",          "넷플릭스"),
    (r"(디즈니\s*\+?|disney\s*\+?)", "디즈니+"),
    (r"(wavve|웨이브)",              "Wavve"),
    (r"(tving|티빙)",               "TVING"),
    (r"(쿠팡\s*플레이|coupang\s*play)", "쿠팡플레이"),
    (r"(apple\s*tv\+?|애플\s*tv\+?)", "Apple TV+"),
]


def _normalize_channel(ch: str) -> str:
    """채널명 정규화: 줄바꿈·괄호 보조정보 제거 후 첫 번째 채널명만 반환.
    예) 'ENA\n(TV)\nGenie TV\n(스트리밍)' → 'ENA'
        'tvN (케이블)' → 'tvN'
    """
    if not ch:
        return ch
    # 줄바꿈 기준으로 첫 번째 토큰만 사용
    first = ch.split("\n")[0].strip()
    # 괄호 안 내용 제거
    import re as _re
    first = _re.sub(r"\s*\([^)]*\)", "", first).strip()
    # 쉼표·슬래시로 여러 채널이 붙은 경우 첫 채널만 ('tvN, TVING' → 'tvN')
    first = _re.split(r"\s*[,/·]\s*", first)[0].strip()
    # 같은 계열의 채널명은 하나로 통일 ('KBS 2TV'·'KBS1' → 'KBS', 'SBS TV' → 'SBS', 'tvN STORY' → 'tvN' …)
    for pattern, canonical in _CHANNEL_ALIASES:
        if _re.match(pattern, first, _re.IGNORECASE):
            return canonical
    return first


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dramas (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                wiki_url        TEXT,
                official_site_url TEXT,
                poster          TEXT,
                description     TEXT,
                channel         TEXT,
                air_date        TEXT,
                total_episodes  TEXT,
                seasons         TEXT,
                director        TEXT,
                writer          TEXT,
                cast_json       TEXT,   -- JSON array of strings
                genre           TEXT,
                rating          TEXT,
                infobox_json    TEXT,   -- JSON object
                external_links_json TEXT, -- JSON array
                og_json         TEXT,   -- JSON object (filtered)
                jsonld_json     TEXT,   -- JSON array
                streaming_json  TEXT,   -- JSON array
                metadata_json   TEXT,   -- JSON object (보고서 형식 구조화 메타데이터)
                wiki_sections_json TEXT, -- JSON array: 위키피디아 전체 본문 섹션 (sections)
                broadcaster     TEXT,   -- SBS | MBC | MBC충북 | CJ | Com2usN 등
                content_type    TEXT,   -- drama | variety | culture | sports | news | all
                crawled_at      TEXT NOT NULL,
                status          TEXT NOT NULL  -- 'ok' | 'no_official' | 'error'
            )
        """)
        # 크롤 진행 상태 테이블
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crawl_queue (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT NOT NULL,
                wiki_url    TEXT,
                queued_at   TEXT NOT NULL,
                started_at  TEXT,
                done_at     TEXT,
                status      TEXT NOT NULL DEFAULT 'pending'
                            -- 'pending' | 'running' | 'done' | 'error'
            )
        """)
        # ── MBC충북 뉴스 기사 테이블 ───────────────────────────────────────────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS news_articles (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source      TEXT NOT NULL DEFAULT 'mbccb',
                title       TEXT NOT NULL,
                url         TEXT,
                summary     TEXT,
                body        TEXT,
                reporter    TEXT,
                published_at TEXT,
                image_url   TEXT,
                video_id    TEXT,
                youtube_url TEXT,
                crawled_at  TEXT NOT NULL
            )
        """)
        # ── 유튜브 영상 테이블 (com2usN 드라마 & MBC충북 다시보기/LIVE) ────
        await db.execute("""
            CREATE TABLE IF NOT EXISTS youtube_videos (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT NOT NULL,
                category        TEXT NOT NULL,
                video_id        TEXT NOT NULL UNIQUE,
                title           TEXT,
                description     TEXT,
                youtube_url     TEXT,
                page_url        TEXT,
                drama_title     TEXT,
                poster          TEXT,   -- YouTube 없는 경우 대표 이미지 URL
                wiki_data       TEXT,   -- JSON: com2usN 드라마 위키피디아 보충 메타데이터
                crawled_at      TEXT NOT NULL
            )
        """)
        await db.commit()

        # ── 기존 DB 마이그레이션 (컬럼 추가) ────────────────────────────────
        for col_def in [
            "ALTER TABLE dramas ADD COLUMN metadata_json TEXT",
            "ALTER TABLE dramas ADD COLUMN broadcaster TEXT",
            "ALTER TABLE dramas ADD COLUMN content_type TEXT",
            "ALTER TABLE crawl_queue ADD COLUMN official_url TEXT",
            "ALTER TABLE crawl_queue ADD COLUMN broadcaster TEXT",
            "ALTER TABLE crawl_queue ADD COLUMN content_type TEXT",
            "ALTER TABLE youtube_videos ADD COLUMN wiki_data TEXT",
            "ALTER TABLE youtube_videos ADD COLUMN poster TEXT",
            "ALTER TABLE news_articles ADD COLUMN image_url TEXT",
            "ALTER TABLE news_articles ADD COLUMN video_id TEXT",
            "ALTER TABLE news_articles ADD COLUMN youtube_url TEXT",
            "ALTER TABLE news_articles ADD COLUMN manual_fields TEXT",   # JSON array: 사용자가 수정한 필드(재수집 보호)
            "ALTER TABLE news_articles ADD COLUMN edit_log TEXT",        # JSON array: 수정 이력
            "ALTER TABLE news_articles ADD COLUMN auto_values TEXT",     # JSON object: 필드별 자동 값(수집이 채운 값) — '자동 값으로' 복원용
            "ALTER TABLE youtube_videos ADD COLUMN manual_fields TEXT",  # 영상(다시보기/LIVE) 수정 보호
            "ALTER TABLE youtube_videos ADD COLUMN edit_log TEXT",
            # url 유니크 인덱스 (중복 방지)
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url ON news_articles(url)",
        ]:
            try:
                await db.execute(col_def)
                await db.commit()
            except Exception:
                pass  # 이미 존재하면 무시

        # 저장된 드라마/예능 metadata_json 을 통일 스키마로 맞춘다 (멱등: 이미 맞는 행은 건드리지 않음)
        try:
            n = await migrate_drama_meta(db)
            if n:
                logging.getLogger(__name__).info(f"[db] metadata_json 통일 스키마 적용: {n}건")
        except Exception as e:
            logging.getLogger(__name__).warning(f"[db] metadata_json 통일 마이그레이션 실패: {e}")

        # 출처별 원본·인물·회차 테이블 (dramas 옆에 붙는 보조 테이블, 멱등)
        try:
            from core.source_tables import ensure_tables
            await ensure_tables(db)
            await db.commit()
        except Exception as e:
            logging.getLogger(__name__).warning(f"[db] 출처 테이블 생성 실패: {e}")


# ── 드라마 저장/조회 ─────────────────────────────────────────────────────────

# SELECT 시 항상 이 순서로 컬럼을 명시해서 ALTER TABLE 순서 문제를 방지한다
_SELECT_COLS = (
    "id, title, wiki_url, official_site_url, poster, "
    "description, channel, air_date, total_episodes, seasons, "
    "director, writer, cast_json, genre, rating, "
    "infobox_json, external_links_json, og_json, jsonld_json, "
    "streaming_json, metadata_json, wiki_sections_json, broadcaster, content_type, crawled_at, status"
)
_SELECT_KEYS = [c.strip() for c in _SELECT_COLS.split(",")]

# '수정됨' 필터: metadata_json.manual_fields 가 비어있지 않은 행 (깨진 JSON 은 제외)
_EDITED_SQL = (
    "(CASE WHEN metadata_json IS NOT NULL AND json_valid(metadata_json) "
    "THEN json_array_length(COALESCE(json_extract(metadata_json, '$.manual_fields'), '[]')) ELSE 0 END) > 0"
)


# ── 드라마/예능 metadata_json 통일 스키마 ──────────────────────────────────────
# 1) 공통 키는 항상 존재한다. 값이 없으면 null(목록 키는 [], 방영 기간은 start/end/raw 가 null 인 객체).
# 2) 위키 원본(인포박스·본문 섹션·외부 링크·요약)과 TVING·tvN 연결 레코드(기본정보·인물·회차·클립)는 `wikipedia` / `tving` / `tvn` 키에
#    통째로 넣는다(없으면 null). 단, 메타 안에서 같은 값이 두 번 나오면 한 곳만 남긴다 (dedupe_full_meta: 공통 키 우선).
#    정본은 각 컬럼·drama_sources 이고, sync_full_meta() 가 저장 때마다 이 키들을 다시 채운다.
# 3) 출처별 고유 필드(source, youtube_url, wiki_hint 등)는 `extra` 하위 객체에 모은다.
DRAMA_META_KEYS = (
    "title", "description", "poster_url", "wikipedia_url", "wiki_status",
    "channel", "additional_channels", "broadcast_country", "origin",
    "broadcast_period", "broadcast_time", "episode_duration", "episode_count",
    "genre", "content_rating",
    "planner", "executive_producer", "general_producer", "producer", "production_company",
    "director", "writer", "cast", "cast_roles", "music",
    "wikipedia", "tving", "tvn",
    "poster_manual", "manual_fields", "edit_log", "auto_values", "extra",
)
_META_LIST_KEYS = ("director", "writer", "cast", "cast_roles", "manual_fields", "edit_log")
DRAMA_META_EXTRA_KEYS = ("source", "youtube_url", "wiki_hint")   # extra 에 항상 존재하는 키 (그 밖의 키도 보존)
_META_DROP_KEYS = ("infobox", "sections")                          # 컬럼이 정본이라 메타에는 두지 않는다


def _nrm(t) -> str:
    return re.sub(r"[\W_]+", "", str(t or "")).lower()


def dedupe_full_meta(meta: dict) -> dict:
    """metadata_json 안에서 서로 겹치는 값을 한 곳에만 남긴다 (in-place). 정보는 잃지 않는다 — 같은 값이 다른 키에 이미 있을 때만 뺀다.

    공통 키(title·description·poster_url·wikipedia_url·channel·broadcast_period·director·writer·cast_roles)가 우선이고,
    wikipedia / tving / tvn 안의 같은 값은 지운다. 줄거리·요약류 텍스트는 같은 문장이 이미 나온 곳(description 이 먼저)에 있으면 뒤쪽을 지운다.
    tving / tvn 의 인물은 cast_roles(합친 목록)에 이미 들어간 사람만 지운다.
    """
    desc_seen = {_nrm(meta.get("description"))} - {""}

    def text_dup(v) -> bool:
        n = _nrm(v)
        if not n:
            return True                        # 빈 문자열도 '값 없음' 이라 둘 필요 없다
        if n in desc_seen:
            return True
        desc_seen.add(n)
        return False

    w = meta.get("wikipedia")
    if isinstance(w, dict):
        if w.get("url") and w.get("url") == meta.get("wikipedia_url"):
            w.pop("url")
        if w.get("poster") and w.get("poster") == meta.get("poster_url"):
            w.pop("poster")

    bp = meta.get("broadcast_period") if isinstance(meta.get("broadcast_period"), dict) else {}
    cr = [e for e in (meta.get("cast_roles") or []) if isinstance(e, dict)]
    cr_actors = {_nrm(e.get("actor")) for e in cr if e.get("actor")}
    cr_details = {_nrm(e.get("detail")) for e in cr if e.get("detail")}
    for key in ("tving", "tvn"):
        r = meta.get(key)
        if not isinstance(r, dict):
            continue
        if r.get("title") == meta.get("title"):
            r.pop("title", None)
        if r.get("channel") and r.get("channel") == meta.get("channel"):
            r.pop("channel")
        if r.get("poster") and r.get("poster") == meta.get("poster_url"):
            r.pop("poster")
        for a, b in (("directors", "director"), ("writers", "writer")):
            if a in r and list(r.get(a) or []) == list(meta.get(b) or []):
                r.pop(a)
        for a, b in (("start_date", "start"), ("end_date", "end")):
            if r.get(a) and r.get(a) == bp.get(b):
                r.pop(a)
        if r.get("year") and bp.get("start") and str(r["year"]) == str(bp["start"])[:4]:
            r.pop("year")
        att = re.sub(r"\D", "", str(r.get("air_time_text") or ""))
        if att and att == re.sub(r"\D", "", str(bp.get("start") or "") + str(bp.get("end") or "")):
            r.pop("air_time_text")
        for tk in ("synopsis", "intent_short", "intro_long"):
            if tk in r and text_dup(r.get(tk)):
                r.pop(tk)
        ppl = r.get("people")
        if isinstance(ppl, list):
            keep = []
            for p in ppl:
                if not isinstance(p, dict):
                    keep.append(p); continue
                if key == "tving":            # 이름만 있는 출연자: 합친 목록에 배우로 들어갔으면 중복
                    dup = p.get("kind") == "cast" and not p.get("detail") and _nrm(p.get("name")) in cr_actors
                else:                          # tvN: 소개글이 합친 목록에 그대로 들어갔으면 중복
                    dup = bool(p.get("detail")) and _nrm(p.get("detail")) in cr_details
                if not dup:
                    keep.append(p)
            r["people"] = keep
    if isinstance(w, dict) and "summary" in w and text_dup(w.get("summary")):
        w.pop("summary")
    return meta


def _is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or v == {} or v == []


def _meta_list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[,\n]", v) if s.strip()]
    if isinstance(v, (list, tuple)):
        return list(v)
    return [v]


# 위키 인포박스에서 온 문자열 필드: 링크·강조 태그 때문에 생긴 '김 , 이' / '( MBC )' 공백을 정리한다 (직접 수정한 필드는 제외)
_META_TIDY_KEYS = ("genre", "broadcast_country", "broadcast_time", "episode_duration", "additional_channels",
                   "content_rating", "planner", "executive_producer", "general_producer", "producer",
                   "production_company", "music")


def normalize_drama_meta(meta, title: str | None = None) -> dict:
    """metadata_json 을 통일 스키마로 맞춘 새 dict 를 돌려준다 (여러 번 적용해도 결과가 같다).

    - 모든 DRAMA_META_KEYS 가 같은 순서로 존재, 값이 없으면 null/[]
    - infobox·sections 는 제거 (컬럼이 정본)
    - source/youtube_url/wiki_hint 와 알 수 없는 키는 extra 로 이동
    """
    src = dict(meta) if isinstance(meta, dict) else {}
    for k in _META_DROP_KEYS:
        src.pop(k, None)

    extra = {k: None for k in DRAMA_META_EXTRA_KEYS}
    old_extra = src.pop("extra", None)
    if isinstance(old_extra, dict):
        extra.update(old_extra)
    for k in list(src.keys()):                       # 최상위에 있던 출처별/알 수 없는 키 → extra
        if k not in DRAMA_META_KEYS:
            v = src.pop(k)
            if not _is_blank(v) or k not in extra:   # 최상위 값이 더 최신이므로 비어 있지 않으면 덮어쓴다
                extra[k] = v
    extra = {k: (None if _is_blank(v) else v) for k, v in extra.items()}

    _mf = src.get("manual_fields")
    _manual_keys = {f for f in _mf if isinstance(f, str)} if isinstance(_mf, list) else set()
    out: dict = {}
    for k in DRAMA_META_KEYS:
        v = src.get(k)
        if k == "extra":
            out[k] = extra
        elif k == "title":
            out[k] = (title if title else v) or None
        elif k == "broadcast_period":
            if isinstance(v, str):
                v = {"raw": v}
            bp = v if isinstance(v, dict) else {}
            out[k] = {f: (None if _is_blank(bp.get(f)) else bp.get(f)) for f in ("start", "end", "raw")}
            if isinstance(out[k]["raw"], str) and k not in _manual_keys:
                from wiki.parse import tidy_text
                out[k]["raw"] = tidy_text(out[k]["raw"])
        elif k in _META_LIST_KEYS:
            out[k] = _meta_list(v)
        elif k == "poster_manual":
            out[k] = None if v is None else bool(v)
        elif k in _META_TIDY_KEYS and isinstance(v, str) and k not in _manual_keys:
            from wiki.parse import tidy_text   # 지연 import (순환 import 방지)
            v = tidy_text(v)
            out[k] = None if _is_blank(v) else v
        elif k == "episode_count":
            if isinstance(v, str) and v.strip().isdigit():
                v = int(v.strip())
            out[k] = None if _is_blank(v) else v
        else:
            out[k] = None if _is_blank(v) else v
    return out


def _index_summary(wiki_url) -> str:
    """위키 덤프 인덱스(wiki_pages)에 저장된 문서 요약. 없거나 인덱스가 없으면 ''."""
    if not wiki_url:
        return ""
    try:
        import sqlite3
        from urllib.parse import unquote
        from core.paths import WIKI_INDEX
        if not WIKI_INDEX.exists():
            return ""
        title = unquote(str(wiki_url).split("/wiki/")[-1].split("?")[0].split("#")[0]).replace("_", " ")
        con = sqlite3.connect(f"file:{WIKI_INDEX}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT summary FROM wiki_pages WHERE title=?", (title,)).fetchone()
        finally:
            con.close()
        return (r[0] or "") if r else ""
    except Exception:
        return ""


def _index_categories(wiki_url):
    """위키 덤프 인덱스(wiki_pages)에 저장된 문서 카테고리 목록. 없거나 인덱스가 없으면 None."""
    if not wiki_url:
        return None
    try:
        import sqlite3
        from urllib.parse import unquote
        from core.paths import WIKI_INDEX
        if not WIKI_INDEX.exists():
            return None
        title = unquote(str(wiki_url).split("/wiki/")[-1].split("?")[0].split("#")[0]).replace("_", " ")
        con = sqlite3.connect(f"file:{WIKI_INDEX}?mode=ro", uri=True)
        try:
            r = con.execute("SELECT categories_json FROM wiki_pages WHERE title=?", (title,)).fetchone()
        finally:
            con.close()
        cats = json.loads(r[0]) if r and r[0] else None
        return cats if isinstance(cats, list) and cats else None
    except Exception:
        return None


async def sync_full_meta(db, drama_id: int, wiki: dict | None = None) -> bool:
    """metadata_json 의 `wikipedia`·`tving`·`tvn`·`cast_roles`·`origin` 을 정본(컬럼·drama_sources)에서 다시 채우고, 메타 안의 중복 값은 한 곳만 남긴다. 바뀌었으면 True.

    wikipedia = {url, summary, poster, categories, infobox, sections, external_links}  (위키 문서가 없으면 null, 없는 하위 값은 null)
    tving / tvn = 해당 출처의 정규화 레코드 전체 (기본정보·인물·회차·클립 등. 연결 안 됐으면 null)
    wiki 를 주면(방금 수집한 위키 결과) 거기서 summary·poster 를 가져오고, 안 주면 기존 값을 유지한다.
    """
    cur = await db.execute(
        "SELECT title, wiki_url, infobox_json, wiki_sections_json, external_links_json, metadata_json "
        "FROM dramas WHERE id=?", (drama_id,))
    row = await cur.fetchone()
    if not row:
        return False
    title, wurl, ibj, wsj, extj, mj = row
    meta = _json_or_none(mj)
    meta = meta if isinstance(meta, dict) else {}
    old_w = meta.get("wikipedia") if isinstance(meta.get("wikipedia"), dict) else {}
    ib, ws, ext = _json_or_none(ibj), _json_or_none(wsj), _json_or_none(extj)
    ib = ib if isinstance(ib, dict) and ib else None
    ws = ws if isinstance(ws, list) and ws else None
    ext = ext if isinstance(ext, list) and ext else None
    wiki = wiki or {}
    if meta.get("wiki_status") != "none" and (wurl or ib or ws):
        summary = wiki.get("summary") or old_w.get("summary") or _index_summary(wurl)
        wikipedia = {
            "url": wurl or None,
            "summary": summary or None,
            "poster": wiki.get("wiki_poster") or old_w.get("poster") or None,
            "categories": wiki.get("categories") or old_w.get("categories") or _index_categories(wurl) or None,
            "infobox": ib, "sections": ws, "external_links": ext,
        }
    else:
        wikipedia = None
    recs = {"tving": None, "tvn": None}
    try:
        cur = await db.execute(
            "SELECT source, record_json FROM drama_sources WHERE drama_id=? AND source IN ('tving','tvn') "
            "ORDER BY COALESCE(confidence,0) DESC, fetched_at DESC", (drama_id,))
        for src, rj in await cur.fetchall():
            if recs[src] is None:                       # 같은 출처가 둘 이상이면 확신도·최신순 첫 번째
                recs[src] = _json_or_none(rj)
    except Exception:                                   # drama_sources 가 아직 없는 DB
        pass
    ctx = dict(meta)                                     # 계산용 전체본 (저장은 아래에서 슬림하게)
    ctx["wikipedia"], ctx["tving"], ctx["tvn"] = wikipedia, recs["tving"], recs["tvn"]
    from services.cast_roles import build_cast_roles    # 위키·tvN·TVING 에 흩어진 '누가 무슨 역'을 한 목록으로 (파생값)
    meta["cast_roles"] = build_cast_roles(ctx)
    from services.origin import detect_origin            # 제작 국가(국내/해외) — 지우지 않고 표시·필터만 한다
    _cur_origin = meta.get("origin")
    if not ("origin" in manual_fields_of(meta) and isinstance(_cur_origin, dict) and _cur_origin.get("group")):
        meta["origin"] = detect_origin(ctx)              # 사용자가 직접 지정한 값(manual_fields)은 그대로 둔다
    meta["wikipedia"], meta["tving"], meta["tvn"] = wikipedia, recs["tving"], recs["tvn"]
    dedupe_full_meta(meta)
    new = normalize_drama_meta(meta, title)
    if json.dumps(new, ensure_ascii=False, sort_keys=True) == json.dumps(_json_or_none(mj) or {}, ensure_ascii=False, sort_keys=True):
        return False
    await db.execute("UPDATE dramas SET metadata_json=? WHERE id=?", (json.dumps(new, ensure_ascii=False), drama_id))
    return True


def clean_infobox(ib):
    """인포박스 값의 각주·위키 마크업·줄바꿈을 정리한다 (이미 정리된 값에 다시 적용해도 같다)."""
    if not isinstance(ib, dict):
        return ib
    from wiki.parse import _clean_infobox_val   # 지연 import (순환 import 방지)
    return {k: (_clean_infobox_val(v) if isinstance(v, str) else v) for k, v in ib.items()}


def canon_wiki_url(url):
    """위키 문서 URL 을 한 가지 표기로 통일한다 (공백→밑줄, 괄호는 그대로, 한글만 퍼센트 인코딩).

    같은 문서가 수집 경로에 따라 '_(드라마)' / '_%28드라마%29' / '%20(드라마)' 로 달리 저장되어
    URL 이 같은 문서끼리 중복 행이 생기던 문제를 막는다. 위키 문서 URL 이 아니면 그대로 돌려준다.
    """
    if not url or not isinstance(url, str):
        return url
    from urllib.parse import quote, unquote
    m = re.match(r"^(https?://[^/]+/wiki/)([^?#]+)", url.strip())
    if not m:
        return url
    return m.group(1) + quote(unquote(m.group(2)).replace(" ", "_"), safe="()")


# 위키 인포박스에서 '빈 항목만' 채우는 metadata 키 (channel/description/poster 는 수집처 값을 우선하므로 제외)
_WIKI_FILL_KEYS = (
    "genre", "broadcast_country", "broadcast_period", "broadcast_time", "episode_duration", "episode_count",
    "additional_channels", "planner", "executive_producer", "general_producer", "producer",
    "production_company", "director", "writer", "cast", "music", "content_rating", "wikipedia_url",
)


def fill_meta_from_wiki(meta: dict, wiki: dict, title: str = "") -> bool:
    """위키 인포박스로 meta 의 비어 있는 항목만 채운다 (제자리 변경, 바뀌었으면 True).

    값이 이미 있는 항목과 사용자가 직접 수정한 항목(manual_fields)은 건드리지 않는다.
    wiki 는 {"infobox": {...}, "wiki_url": ...} 모양이면 된다.
    """
    from crawlers.metadata import _build_metadata   # 지연 import (순환 import 방지)
    if not isinstance(meta, dict) or not isinstance(wiki, dict) or not wiki.get("infobox"):
        return False
    protected = {k for f in manual_fields_of(meta) for k in _EDIT_FIELD_MAP[f][1]}
    built = _build_metadata(title or meta.get("title") or "", wiki)
    built["wikipedia_url"] = canon_wiki_url(wiki.get("wiki_url")) or None
    changed = False
    for k in _WIKI_FILL_KEYS:
        if k in protected:
            continue
        new, cur = built.get(k), meta.get(k)
        if k == "broadcast_period":
            has = lambda bp: isinstance(bp, dict) and any(bp.get(f) for f in ("start", "end", "raw"))
            if has(cur) or not has(new):
                continue
        elif not _is_blank(cur) or _is_blank(new):
            continue
        meta[k] = new
        changed = True
    return changed


def _json_or_none(s):
    if s is None or isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return None


async def find_drama_id_by_wiki_url(wiki_url):
    """같은 위키 문서(표기 차이 무시)를 가진 저장된 작품의 id. 없으면 None. 오래된 id 우선."""
    target = canon_wiki_url(wiki_url)
    if not target:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, wiki_url FROM dramas WHERE wiki_url IS NOT NULL AND wiki_url != '' ORDER BY id")
        for rid, u in await cur.fetchall():
            if canon_wiki_url(u) == target:
                return rid
    return None


async def migrate_drama_meta(db) -> int:
    """저장된 모든 드라마 행을 통일 스키마로 맞춘다. 멱등(이미 맞는 행은 건드리지 않음). 바뀐 행 수 반환.

    - 메타에 있던 infobox/sections 는 컬럼으로 옮기고(컬럼이 비었거나 메타 쪽이 더 정리된 값일 때) 메타에서는 제거
    - 컬럼에만 있던 인포박스도 같은 방식으로 정리(줄바꿈·각주 제거)
    """
    cur = await db.execute(
        "SELECT id, title, metadata_json, infobox_json, wiki_sections_json, broadcaster, wiki_url, description FROM dramas")
    rows = await cur.fetchall()
    changed = 0
    for rid, title, mj, ibj, wsj, bcast, wurl, desc in rows:
        meta = _json_or_none(mj)
        meta = meta if isinstance(meta, dict) else {}
        manual = {f for f in (meta.get("manual_fields") or []) if isinstance(f, str)}
        col_ib, col_ws = _json_or_none(ibj), _json_or_none(wsj)
        upd: dict = {}

        # 인포박스: 사용자가 직접 고친 값은 그대로 두고, 아니면 (정리된 쪽인) 메타 값 > 컬럼 값 순으로 채택
        m_ib = meta.get("infobox")
        if "infobox" in manual:
            new_ib = col_ib if isinstance(col_ib, dict) and col_ib else m_ib
        else:
            new_ib = m_ib if isinstance(m_ib, dict) and m_ib else col_ib
            new_ib = clean_infobox(new_ib)
        if isinstance(new_ib, dict) and new_ib and new_ib != col_ib:
            upd["infobox_json"] = json.dumps(new_ib, ensure_ascii=False)

        # 위키 섹션: 컬럼이 정본. 컬럼이 비어 있을 때만 메타 값을 옮긴다.
        m_ws = meta.get("sections")
        if isinstance(m_ws, list) and m_ws and not (isinstance(col_ws, list) and col_ws):
            upd["wiki_sections_json"] = json.dumps(m_ws, ensure_ascii=False)
            col_ws = m_ws
        # 본문 섹션 정리(표 중복 텍스트·어색한 공백·빈 '이름 :' 항목). 사용자가 직접 고친 섹션은 그대로 둔다.
        if isinstance(col_ws, list) and col_ws and "wiki_sections" not in manual:
            from wiki.parse import clean_sections
            cleaned_ws = clean_sections(col_ws)
            if cleaned_ws != col_ws:
                upd["wiki_sections_json"] = json.dumps(cleaned_ws, ensure_ascii=False)

        new_meta = normalize_drama_meta(meta, title)
        if new_meta["extra"].get("source") is None and "com2us" in (bcast or "").lower():
            new_meta["extra"]["source"] = "com2usn"   # 새로 저장되는 com2usN 행과 같은 표기

        # 위키 URL 표기 통일 (사용자가 직접 고친 URL 은 그대로 둔다)
        if wurl and "wiki_url" not in manual and canon_wiki_url(wurl) != wurl:
            upd["wiki_url"] = canon_wiki_url(wurl)
        # wiki_status: 위키 URL 이 있는데 비어 있으면 'ok', 안내문만 있으면 'none'
        if new_meta.get("wiki_status") is None and "wiki_status" not in manual:
            if wurl:
                new_meta["wiki_status"] = "ok"
            elif desc == _NO_WIKI_DESC:
                new_meta["wiki_status"] = "none"
        # 저장된 인포박스로 비어 있는 항목(방송 기간·시간·국가·제작사 등)을 채운다
        _ib = new_ib if isinstance(new_ib, dict) and new_ib else col_ib
        if isinstance(_ib, dict) and _ib:
            fill_meta_from_wiki(new_meta, {"infobox": _ib, "wiki_url": wurl}, title)

        if new_meta != meta:
            upd["metadata_json"] = json.dumps(new_meta, ensure_ascii=False)
        if upd:
            sets = ", ".join(f"{k}=?" for k in upd)
            await db.execute(f"UPDATE dramas SET {sets} WHERE id=?", [*upd.values(), rid])
            changed += 1
    try:                                     # 위키 원본·TVING·tvN 레코드를 metadata_json 에 채운다 (바뀐 행만 갱신)
        cur = await db.execute("SELECT id FROM dramas")
        for (rid,) in await cur.fetchall():
            if await sync_full_meta(db, rid):
                changed += 1
    except Exception as e:
        logging.getLogger(__name__).warning(f"[db] metadata 전체 데이터 채우기 실패: {e}")
    await db.commit()
    return changed


def _drama_from_row(row) -> dict:
    d = dict(zip(_SELECT_KEYS, row))
    # JSON 필드 파싱
    for key in ("cast_json", "infobox_json", "external_links_json", "og_json", "jsonld_json", "streaming_json", "metadata_json", "wiki_sections_json"):
        try:
            d[key] = json.loads(d[key] or "null")
        except Exception:
            d[key] = None
    # 로고/배너 URL이면 poster 비우기 (단, 사용자가 직접 입력한 포스터는 그대로 사용)
    _m = d.get("metadata_json") if isinstance(d.get("metadata_json"), dict) else {}
    d["poster"] = (d.get("poster") or "") if _m.get("poster_manual") else _clean_poster(d.get("poster") or "")
    # 프론트엔드용 별칭: wiki_sections_json → wiki_sections
    d["wiki_sections"] = d.get("wiki_sections_json") or []
    return d


def infer_content_type(wiki: dict | None) -> str:
    """위키 문서 분류 결과로 'drama' | 'variety' 를 추정. 판단 불가면 ''.

    수동 검색처럼 content_type 이 넘어오지 않는 경로에서, 저장 대상(드라마·예능)인지
    가르는 기준으로도 쓴다."""
    wiki = wiki or {}
    kind = wiki.get("_dump_class")
    if kind in ("drama", "variety"):
        return kind
    if wiki.get("is_variety") and not wiki.get("is_drama"):
        return "variety"
    if wiki.get("is_drama"):
        return "drama"
    return ""


async def upsert_drama(data: dict) -> tuple[int, bool]:
    """크롤 결과를 DB에 저장 (같은 제목이면 업데이트).
    반환: (drama_id, is_new) — is_new=True이면 신규 INSERT, False이면 기존 UPDATE

    위키피디아 기반 메타데이터 형식 사용.
    metadata_json 에 _build_metadata 결과를 그대로 저장하고
    자주 검색/필터링되는 필드는 별도 컬럼에도 저장한다.
    """
    wiki    = data.get("wikipedia", {})
    infobox = wiki.get("infobox", {})
    meta    = data.get("metadata", {})   # _build_metadata 결과
    now     = datetime.utcnow().isoformat()

    # ── 핵심 필드 추출 (metadata 우선, wiki 인포박스 폴백) ────────────────────
    title       = data.get("title", "")
    wiki_url    = canon_wiki_url(wiki.get("wiki_url")) or None  # 위키피디아 URL만 저장(표기 통일), official_site_url 혼용 금지
    poster      = _clean_poster(meta.get("poster_url") or "") or _clean_poster(wiki.get("wiki_poster") or "")
    # 위키피디아 없음(no_wiki) 명시적 처리
    _no_wiki = bool(wiki.get("no_wiki"))
    if _no_wiki:
        description = meta.get("description") or "위키피디아 문서가 없습니다"
    else:
        description = meta.get("description") or wiki.get("summary") or ""

    # 채널 (첫 번째 채널만, 줄바꿈·괄호 제거)
    channel = _normalize_channel(meta.get("channel") or "")
    if meta.get("channel"):
        meta["channel"] = channel   # 상세 모달이 meta.channel 을 우선 표시하므로 같은 표기로 맞춘다

    # 위키 상태를 metadata_json 에 명시 (카드/모달의 '위키피디아 문서 없음' 표시에 사용)
    if _no_wiki:
        meta["wiki_status"] = "none"
    elif wiki.get("wiki_url") or wiki.get("summary"):
        meta["wiki_status"] = "ok"

    # 방영 기간 → air_date에 시작일만 저장
    period   = meta.get("broadcast_period") or {}
    air_date = period.get("start") or period.get("raw") or \
               infobox.get("방송 기간") or infobox.get("방영 기간") or ""
    # 위키 한 문서에 시즌별 기간이 이어 적힌 작품('~2', '시즌N')은 해당 시즌의 기간을 고른다
    from services.work_matcher import split_season as _split_season, season_period as _season_period
    _sp = _season_period(period.get("raw") or infobox.get("방송 기간") or infobox.get("방영 기간") or "",
                         _split_season(title or "")[1])
    if _sp:
        meta["broadcast_period"] = _sp
        air_date = _sp["start"]

    # 에피소드 수
    ep_count = meta.get("episode_count")
    episodes = str(ep_count) if ep_count is not None else \
               infobox.get("방송 횟수") or infobox.get("편수") or ""

    # 연출/극본 (리스트 → 쉼표 문자열)
    director = ", ".join(meta.get("director") or []) or \
               infobox.get("연출") or infobox.get("감독") or ""
    writer   = ", ".join(meta.get("writer") or []) or \
               infobox.get("극본") or infobox.get("각본") or ""

    # 출연진 (리스트)
    cast  = meta.get("cast") or []
    genre = meta.get("genre") or infobox.get("장르") or ""
    rating = meta.get("content_rating") or infobox.get("방송 등급") or ""

    broadcaster  = data.get("broadcaster", "")
    content_type = data.get("content_type", "")
    # 수동 검색 등 content_type 이 안 넘어온 경우: 위키 분류(덤프 → 카테고리)로 보충
    if not content_type and not _no_wiki:
        content_type = infer_content_type(wiki)

    # broadcaster가 com2usN이면 channel도 com2usN으로 설정
    if broadcaster and "com2us" in broadcaster.lower() and not channel:
        channel = broadcaster

    # ── 상태 결정 ─────────────────────────────────────────────────────────────
    # official_site_url은 아직 확정되지 않았으므로 wiki 유무만 보고 임시 결정
    # 최종 status는 upsert 후 official_site_url 존재 여부로 결정됨
    _has_wiki = bool(wiki.get("wiki_url") or wiki.get("summary")) and not _no_wiki
    if data.get("error"):
        status = "error"
    else:
        status = "ok" if _has_wiki else "no_official"

    async with aiosqlite.connect(DB_PATH) as db:
        # 신규 컬럼 마이그레이션 (기존 DB 호환)
        for col_def in [
            "ALTER TABLE dramas ADD COLUMN metadata_json TEXT",
            "ALTER TABLE dramas ADD COLUMN wiki_sections_json TEXT",
            "ALTER TABLE dramas ADD COLUMN broadcaster TEXT",
            "ALTER TABLE dramas ADD COLUMN content_type TEXT",
        ]:
            try:
                await db.execute(col_def)
                await db.commit()
            except Exception:
                pass  # 이미 존재하면 무시

        # ── 중복 방지: BEGIN IMMEDIATE로 DB 락 후 존재 확인 (race condition 방지) ──
        await db.execute("BEGIN IMMEDIATE")
        # 같은 작품이 수집처마다 띄어쓰기만 다르게 오는 경우('닥터 X' / '닥터X')도 같은 행으로 본다.
        # 후보가 여럿이면 제목이 정확히 같은 행 → 공백 무시 일치 → 위키 URL 일치 순, 그 안에서는 오래된 id 우선.
        _title_key = (title or "").replace(" ", "")
        cur = await db.execute(
            """SELECT id FROM dramas
               WHERE title = ?
                  OR REPLACE(title, ' ', '') = ?
               ORDER BY (title = ?) DESC, (REPLACE(title, ' ', '') = ?) DESC, id ASC
               LIMIT 1""",
            (title, _title_key, title, _title_key),
        )
        existing = await cur.fetchone()
        if not existing and wiki_url:
            # 제목이 달라도('신병' / '신병1') 같은 위키 문서면 같은 작품. URL 표기가 달라도 같은 문서로 본다.
            # 단, 시즌이 다르면('유미의 세포들 시즌2' / '유미의 세포들 시즌3') 위키 문서가 같아도 다른 작품이다.
            from services.work_matcher import split_season, season_compatible
            _new_season = split_season(title)[1]
            cur = await db.execute("SELECT id, wiki_url, title FROM dramas WHERE wiki_url IS NOT NULL AND wiki_url != '' ORDER BY id")
            for _rid, _u, _rt in await cur.fetchall():
                if canon_wiki_url(_u) == wiki_url and season_compatible(split_season(_rt)[1], _new_season):
                    existing = (_rid,)
                    break

        # metadata_json: 통일 스키마로 정규화해 저장 (인포박스·섹션은 아래 컬럼에만 저장, 출처별 필드는 extra)
        meta_json_str = json.dumps(normalize_drama_meta(meta, title), ensure_ascii=False)
        # infobox_json: 위키피디아 인포박스 (줄바꿈·각주를 정리한 값. 인포박스의 유일한 저장 위치)
        infobox_json_str = json.dumps(clean_infobox(wiki.get("infobox_raw") or infobox), ensure_ascii=False)
        # wiki_sections_json: 위키피디아 전체 본문 섹션 (중복 없이)
        wiki_sections = wiki.get("sections") or []
        if wiki_sections:
            from wiki.parse import clean_sections
            wiki_sections = clean_sections(wiki_sections)
        wiki_sections_json_str = json.dumps(wiki_sections, ensure_ascii=False) if wiki_sections else None

        # official_site_url: data 직접값 → wiki external_links 순으로 폴백
        official_site_url = data.get("official_site_url") or None
        if not official_site_url:
            _ext = wiki.get("external_links") or []
            for _lnk in _ext:
                _u = _lnk.get("url") if isinstance(_lnk, dict) else (_lnk if isinstance(_lnk, str) else "")
                if _u and _u.startswith("http"):
                    official_site_url = _u
                    break

        if existing:
            drama_id = existing[0]
            # 기존 official_site_url이 있고 새 값이 없으면 기존 값 유지
            _prev = await db.execute("SELECT official_site_url FROM dramas WHERE id=?", (drama_id,))
            _prev_row = await _prev.fetchone()
            _prev_official = _prev_row[0] if _prev_row else None
            final_official = official_site_url or _prev_official
            # 사용자가 직접 수정한 항목(manual_fields)은 재수집 결과로 덮어쓰지 않고 기존 값을 유지
            cast_json_str = json.dumps(cast, ensure_ascii=False)
            _nc = {"wiki_url": wiki_url, "official_site_url": final_official, "poster": poster,
                   "description": description, "channel": channel, "air_date": air_date,
                   "total_episodes": episodes, "director": director, "writer": writer,
                   "cast_json": cast_json_str, "genre": genre, "rating": rating,
                   "infobox_json": infobox_json_str, "wiki_sections_json": wiki_sections_json_str,
                   "broadcaster": broadcaster, "content_type": content_type}
            _nm = json.loads(meta_json_str)
            if await restore_manual_fields(db, drama_id, _nc, _nm):
                wiki_url, final_official, poster = _nc["wiki_url"], _nc["official_site_url"], _nc["poster"]
                description, channel, air_date = _nc["description"], _nc["channel"], _nc["air_date"]
                episodes, director, writer = _nc["total_episodes"], _nc["director"], _nc["writer"]
                cast_json_str, genre, rating = _nc["cast_json"], _nc["genre"], _nc["rating"]
                infobox_json_str, wiki_sections_json_str = _nc["infobox_json"], _nc["wiki_sections_json"]
                broadcaster, content_type = _nc["broadcaster"], _nc["content_type"]
            # 보호 항목이 없어도 수정 이력·extra 이월 결과가 _nm 에 들어 있으므로 항상 다시 직렬화한다
            meta_json_str = json.dumps(normalize_drama_meta(_nm), ensure_ascii=False)   # title 은 restore 결과(수정한 제목 포함) 유지
            await db.execute("""
                UPDATE dramas SET
                    wiki_url=?, official_site_url=?, poster=?, description=?,
                    channel=?, air_date=?, total_episodes=?, seasons=?,
                    director=?, writer=?, cast_json=?, genre=?, rating=?,
                    infobox_json=?, external_links_json=?, og_json=?,
                    jsonld_json=?, streaming_json=?, metadata_json=?,
                    wiki_sections_json=?,
                    broadcaster=?, content_type=?,
                    crawled_at=?, status=?
                WHERE id=?
            """, (
                wiki_url, final_official, poster, description,
                channel, air_date, episodes, "",
                director, writer, cast_json_str,
                genre, rating,
                infobox_json_str,
                json.dumps(wiki.get("external_links", []), ensure_ascii=False),
                "{}", "[]", "[]",
                meta_json_str,
                wiki_sections_json_str,
                broadcaster, content_type,
                now, status,
                drama_id,
            ))
        else:
            cur = await db.execute("""
                INSERT INTO dramas (
                    title, wiki_url, official_site_url, poster, description,
                    channel, air_date, total_episodes, seasons,
                    director, writer, cast_json, genre, rating,
                    infobox_json, external_links_json, og_json,
                    jsonld_json, streaming_json, metadata_json,
                    wiki_sections_json,
                    broadcaster, content_type, crawled_at, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                title, wiki_url, official_site_url,
                poster, description, channel, air_date, episodes, "",
                director, writer, json.dumps(cast, ensure_ascii=False),
                genre, rating,
                infobox_json_str,
                json.dumps(wiki.get("external_links", []), ensure_ascii=False),
                "{}", "[]", "[]",
                meta_json_str,
                wiki_sections_json_str,
                broadcaster, content_type,
                now, status,
            ))
            drama_id = cur.lastrowid
            await sync_full_meta(db, drama_id, wiki)
            await db.commit()
            return drama_id, True

        await sync_full_meta(db, drama_id, wiki)
        await db.commit()
        return drama_id, False


# ── 수정(편집) ────────────────────────────────────────────────────────────────
# 카드는 컬럼을, 모달은 metadata_json 을 우선 읽기 때문에 수정은 항상 둘 다 갱신해야 한다.

_EDIT_CONTENT_TYPES = {"", "drama", "variety", "culture", "sports", "news", "all"}
# metadata_json 에만 저장되는 텍스트 필드 (모달 '방송 정보'/'제작 정보' 에 표시)
EDIT_META_TEXT_KEYS = (
    "additional_channels", "broadcast_country", "broadcast_time", "episode_duration",
    "planner", "executive_producer", "general_producer", "producer", "production_company", "music",
)
_NO_WIKI_DESC = "위키피디아 문서가 없습니다"

# '공식사이트 보유' 기준: 카드/모달의 '🌐 공식 사이트' 버튼이 나타나는 조건(official_site_url 이 비어 있지 않음)과 같다.
# 상단 통계 칩(get_stats)·목록 필터가 모두 이 한 정의를 쓴다.
_HAS_OFFICIAL_SQL = "(official_site_url IS NOT NULL AND official_site_url != '')"

# 수정 항목 → (dramas 컬럼, metadata_json 키). 재수집 보호(manual_fields)에서 되돌릴 대상을 정한다.
_EDIT_FIELD_MAP: dict[str, tuple[list[str], list[str]]] = {
    "title":             ([], ["title"]),   # 제목 컬럼은 upsert 가 건드리지 않는다
    "channel":           (["channel"], ["channel"]),
    "genre":             (["genre"], ["genre"]),
    "description":       (["description"], ["description"]),
    "rating":            (["rating"], ["content_rating"]),
    "poster":            (["poster"], ["poster_url", "poster_manual"]),
    "wiki_url":          (["wiki_url"], ["wikipedia_url"]),
    "official_site_url": (["official_site_url"], []),
    "director":          (["director"], ["director"]),
    "writer":            (["writer"], ["writer"]),
    "cast":              (["cast_json"], ["cast"]),
    "total_episodes":    (["total_episodes"], ["episode_count"]),
    "broadcast_period":  (["air_date"], ["broadcast_period"]),
    "content_type":      (["content_type"], []),
    "broadcaster":       (["broadcaster"], []),
    "infobox":           (["infobox_json"], []),        # 인포박스·섹션은 컬럼에만 저장한다
    "wiki_sections":     (["wiki_sections_json"], []),
    "wiki_status":       ([], ["wiki_status"]),
    "origin":            ([], ["origin"]),              # 제작 국가(국내/해외) 수동 지정 — 지정하면 자동 판별이 덮어쓰지 않는다
    **{k: ([], [k]) for k in EDIT_META_TEXT_KEYS},
}
# 수정 이력에 표시할 한글 항목명
_EDIT_LABELS = {
    "title": "제목", "channel": "방송 채널", "genre": "장르", "description": "설명", "rating": "등급",
    "poster": "포스터", "wiki_url": "위키 URL", "official_site_url": "공식 사이트",
    "director": "연출", "writer": "극본", "cast": "출연진", "total_episodes": "총 화수",
    "broadcast_period": "방영 기간", "content_type": "분류", "broadcaster": "방송사",
    "infobox": "인포박스", "wiki_sections": "위키 섹션", "wiki_status": "위키 문서", "origin": "제작 국가",
    "additional_channels": "추가 채널", "broadcast_country": "방송 국가", "broadcast_time": "방송 시간",
    "episode_duration": "방송 분량", "planner": "기획", "executive_producer": "책임프로듀서",
    "general_producer": "제작총괄", "producer": "프로듀서", "production_company": "제작사", "music": "음악",
}
_LEGACY_META_KEY = {"infobox_json": "infobox", "wiki_sections_json": "sections"}   # 통일 이전에 메타에 중복 저장되던 키
_RESTORE_COLS = ["wiki_url", "official_site_url", "poster", "description", "channel", "air_date",
                 "total_episodes", "director", "writer", "cast_json", "genre", "rating",
                 "infobox_json", "wiki_sections_json", "broadcaster", "content_type"]


def manual_fields_of(meta) -> set[str]:
    """metadata_json 에서 '사용자가 직접 수정한 항목' 집합을 꺼낸다."""
    mf = meta.get("manual_fields") if isinstance(meta, dict) else None
    return {f for f in (mf or []) if f in _EDIT_FIELD_MAP}


def _norm_for_compare(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v).strip()


_RAW_CAP = 20000   # 이력·자동값에 원본을 통째로 저장하는 최대 크기(JSON 문자열 길이). 넘으면 저장하지 않는다(되돌리기 불가)


def _cap_raw(v):
    """이력에 저장할 원본 값. 너무 크면 _MISSING 을 돌려 저장을 생략한다."""
    try:
        return v if len(json.dumps(v, ensure_ascii=False)) <= _RAW_CAP else _MISSING
    except Exception:
        return _MISSING


_MISSING = object()


def _field_body_value(f: str, cols: dict, meta: dict, title: str):
    """수정 항목 f 의 현재 값을 'apply_drama_edit 의 body 형식'으로 뽑는다(되돌리기·자동값 복원에 그대로 쓴다)."""
    g = cols.get
    if f == "title":
        return title
    if f in ("channel", "genre", "description", "rating", "poster", "wiki_url", "official_site_url",
             "total_episodes", "content_type", "broadcaster"):
        return str(g(f) or "")
    if f in ("director", "writer"):
        lst = meta.get(f)
        return list(lst) if isinstance(lst, list) and lst else _edit_list(g(f))
    if f == "cast":
        lst = meta.get("cast")
        if isinstance(lst, list) and lst:
            return list(lst)
        try:
            lst = json.loads(g("cast_json") or "[]")
        except Exception:
            lst = []
        return lst if isinstance(lst, list) else []
    if f == "broadcast_period":
        bp = meta.get("broadcast_period")
        if isinstance(bp, dict) and any(bp.get(k) for k in ("start", "end", "raw")):
            return {k: str(bp.get(k) or "") for k in ("start", "end", "raw")}
        return {"start": str(g("air_date") or ""), "end": "", "raw": ""}
    if f in ("infobox", "wiki_sections"):
        col = _EDIT_FIELD_MAP[f][0][0]
        v = g(col)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = None
        v = v or meta.get(_LEGACY_META_KEY[col])
        return v if v else ({} if f == "infobox" else [])
    if f == "wiki_status":
        return bool(meta.get("wiki_status") == "none" or g("description") == _NO_WIKI_DESC)
    if f == "origin":
        o = meta.get("origin")
        if isinstance(o, dict) and o.get("basis") == "manual" and o.get("group") in ("domestic", "foreign"):
            return {"group": o["group"], "country": o.get("country") or ""}
        return {"group": "auto", "country": ""}
    return str(meta.get(f) or "")


def _body_from_field(f: str, v) -> dict:
    """_field_body_value 로 뽑은 값 → apply_drama_edit 에 넘길 body 조각."""
    if f == "wiki_status":
        return {"wiki_no_doc": bool(v)}
    if f in EDIT_META_TEXT_KEYS:
        return {"meta": {f: v}}
    return {f: v}


async def restore_manual_fields(db, drama_id: int, new_cols: dict, new_meta: dict) -> set[str]:
    """재수집으로 만든 new_cols/new_meta 중 사용자가 수정한 항목은 기존 값으로 되돌린다(제자리 변경)."""
    cur = await db.execute(
        f"SELECT {', '.join(_RESTORE_COLS)}, metadata_json FROM dramas WHERE id=?", (drama_id,))
    row = await cur.fetchone()
    if not row:
        return set()
    prev = dict(zip(_RESTORE_COLS, row[:-1]))
    try:
        prev_meta = json.loads(row[-1] or "{}") or {}
    except Exception:
        prev_meta = {}
    prev_meta = normalize_drama_meta(prev_meta)   # 예전 형식(최상위 source 등)으로 남은 행도 같은 구조로 읽는다
    if prev_meta.get("edit_log"):          # 재수집이 수정 이력을 지우지 않도록 이월 (보호 항목이 없어도)
        new_meta["edit_log"] = prev_meta["edit_log"]
    # 출처·위키 힌트는 위키 검색 등 힌트를 모르는 수집 경로로 다시 저장해도 사라지지 않게 기존 값을 유지한다
    # (youtube_url 은 영상이 바뀔 수 있으므로 이월하지 않는다)
    new_extra = new_meta.get("extra") if isinstance(new_meta.get("extra"), dict) else {}
    for k in ("source", "wiki_hint"):
        if _is_blank(new_extra.get(k)) and not _is_blank((prev_meta.get("extra") or {}).get(k)):
            new_extra[k] = prev_meta["extra"][k]
    new_meta["extra"] = new_extra
    manual = manual_fields_of(prev_meta)
    if not manual:
        return set()
    # 재수집이 새로 가져온 값은 버리기 전에 '자동 값'으로 보관해 둔다 (나중에 '자동 값으로' 복원할 때 최신 수집값을 쓴다)
    auto_vals = dict(prev_meta.get("auto_values") or {}) if isinstance(prev_meta.get("auto_values"), dict) else {}
    _blank_cols = {c: None for c in _RESTORE_COLS}
    for f in manual:
        if f in ("title", "wiki_status", "origin"):
            continue
        fcols, mkeys = _EDIT_FIELD_MAP[f]
        if (fcols and fcols[0] not in new_cols) or (not fcols and not (mkeys and mkeys[0] in new_meta)):
            continue
        try:
            fresh = _field_body_value(f, {**_blank_cols, **new_cols}, new_meta, "")
        except Exception:
            continue
        if not _is_blank(fresh) and _cap_raw(fresh) is not _MISSING:
            auto_vals[f] = fresh
    for f in manual:
        cols, mkeys = _EDIT_FIELD_MAP[f]
        for c in cols:
            if c in new_cols:
                new_cols[c] = prev[c]
        for k in mkeys:
            if k in prev_meta:
                new_meta[k] = prev_meta[k]
            else:
                new_meta.pop(k, None)
    new_meta["manual_fields"] = sorted(manual)
    new_meta["auto_values"] = {k: v for k, v in auto_vals.items() if k in manual} or None
    if prev_meta.get("edit_log"):          # 재수집이 수정 이력을 지우지 않도록 이월
        new_meta["edit_log"] = prev_meta["edit_log"]
    return manual


def _edit_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = re.split(r"[,\n]", v)
    return [str(x).strip() for x in v if str(x).strip()]


async def apply_drama_edit(drama_id: int, body: dict, *, release: set[str] | None = None,
                           log_note: str = "") -> list[str]:
    """수정 내용을 컬럼 + metadata_json 에 함께 반영한다. body 에 있는 키만 수정(빈 값 = 비우기).

    release: 저장 후 '직접 수정됨' 표시(재수집 보호)를 풀 항목 — '자동 값으로 복원'에서 쓴다.
    log_note: 이번 저장으로 생기는 이력 줄에 붙일 설명(예: '이력 되돌림').

    예외: LookupError(없는 id) / FileExistsError(제목 중복) / ValueError(잘못된 값)
    반환: 수정된 body 키 목록
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT title, metadata_json, wiki_url, description, " + ", ".join(_RESTORE_COLS) +
            " FROM dramas WHERE id=?", (drama_id,))
        row = await cur.fetchone()
        if not row:
            raise LookupError(drama_id)
        old_title, old_wiki_url, old_desc = row[0], row[2], row[3]
        old_cols = dict(zip(_RESTORE_COLS, row[4:]))
        try:
            meta = json.loads(row[1] or "{}") or {}
        except Exception:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        old_meta = json.loads(json.dumps(meta))   # 변경 감지용 사본

        cols: dict = {}
        has = body.__contains__

        def _s(k) -> str:
            return str(body.get(k) if body.get(k) is not None else "").strip()

        if has("title"):
            t = _s("title")
            if not t:
                raise ValueError("제목은 비울 수 없습니다.")
            dup = await db.execute("SELECT id FROM dramas WHERE title=? AND id<>?", (t, drama_id))
            if await dup.fetchone():
                raise FileExistsError(t)
            cols["title"] = t
            meta["title"] = t

        for key, col, mkey in (("channel", "channel", "channel"), ("genre", "genre", "genre"),
                               ("description", "description", "description"),
                               ("rating", "rating", "content_rating")):
            if has(key):
                v = _s(key)
                cols[col] = v
                meta[mkey] = v or None

        if has("poster"):
            v = _s("poster")
            cols["poster"] = v
            meta["poster_url"] = v or None
            meta["poster_manual"] = bool(v)   # 직접 입력한 포스터는 로고 필터로 숨기지 않는다

        if has("wiki_url"):
            v = _s("wiki_url")
            cols["wiki_url"] = canon_wiki_url(v) or None
            meta["wikipedia_url"] = v or None
        if has("official_site_url"):
            cols["official_site_url"] = _s("official_site_url") or None

        for key in ("director", "writer"):
            if has(key):
                lst = _edit_list(body[key])
                cols[key] = ", ".join(lst)
                meta[key] = lst
        cast_key = "cast" if has("cast") else ("cast_json" if has("cast_json") else None)
        if cast_key:
            lst = _edit_list(body[cast_key])
            cols["cast_json"] = json.dumps(lst, ensure_ascii=False)
            meta["cast"] = lst

        if has("total_episodes"):
            v = _s("total_episodes")
            cols["total_episodes"] = v
            meta["episode_count"] = int(v) if v.isdigit() else (v or None)

        if isinstance(body.get("broadcast_period"), dict):
            bp_in = body["broadcast_period"]
            start = str(bp_in.get("start") or "").strip()
            end = str(bp_in.get("end") or "").strip()
            raw = str(bp_in.get("raw") or "").strip()
            if start or end or raw:
                raw = raw or (f"{start} ~ {end}" if start and end else (start or end))
                meta["broadcast_period"] = {"start": start, "end": end, "raw": raw}
            else:
                meta["broadcast_period"] = {}
            cols["air_date"] = start or raw
        elif has("air_date"):
            a = _s("air_date")
            cols["air_date"] = a
            meta["broadcast_period"] = {"start": a, "end": "", "raw": a} if a else {}

        if has("content_type"):
            ct = _s("content_type")
            if ct not in _EDIT_CONTENT_TYPES:
                raise ValueError(f"잘못된 분류: {ct}")
            cols["content_type"] = ct
        if has("broadcaster"):
            cols["broadcaster"] = _s("broadcaster")

        if isinstance(body.get("meta"), dict):
            for k in EDIT_META_TEXT_KEYS:
                if k in body["meta"]:
                    meta[k] = str(body["meta"][k] or "").strip() or None

        if has("infobox"):
            if not isinstance(body["infobox"], dict):
                raise ValueError("인포박스는 JSON 객체여야 합니다.")
            cols["infobox_json"] = json.dumps(body["infobox"], ensure_ascii=False)   # 컬럼만 갱신 (메타에는 두지 않음)
        if has("wiki_sections"):
            secs = body["wiki_sections"]
            if not isinstance(secs, list):
                raise ValueError("위키 섹션은 JSON 배열이어야 합니다.")
            cols["wiki_sections_json"] = json.dumps(secs, ensure_ascii=False) if secs else None

        origin_auto = False
        if has("origin"):
            _og = body["origin"] if isinstance(body["origin"], dict) else {"group": body["origin"]}
            _g = str(_og.get("group") or "auto").strip()
            if _g == "auto":
                origin_auto = True       # 자동 판별로 되돌림 (아래에서 수동 표시를 풀고 다시 계산)
            elif _g in ("domestic", "foreign"):
                _c = str(_og.get("country") or "").strip() or None
                if _g == "domestic":
                    _c = _c or "대한민국"
                meta["origin"] = {"group": _g, "country": _c, "basis": "manual", "confidence": "high"}
            else:
                raise ValueError(f"잘못된 제작 국가 값: {_g}")

        if has("wiki_no_doc"):
            if body["wiki_no_doc"]:
                meta["wiki_status"] = "none"
            else:
                eff_wiki = cols["wiki_url"] if "wiki_url" in cols else old_wiki_url
                if eff_wiki:
                    meta["wiki_status"] = "ok"
                else:
                    meta.pop("wiki_status", None)
                eff_desc = cols["description"] if "description" in cols else old_desc
                if eff_desc == _NO_WIKI_DESC:   # '문서 없음' 안내문이 설명으로 남아 배지가 계속 뜨는 것 방지
                    cols["description"] = ""
                    meta["description"] = None

        release = set(release or ())
        if not cols and not has("wiki_no_doc") and not has("origin") and not isinstance(body.get("meta"), dict) and not release:
            raise ValueError("수정할 필드가 없습니다.")

        # ── 재수집 보호: 이번 저장으로 '실제로 값이 바뀐' 항목만 manual_fields 에 누적 ──
        touched = {"cast" if k == "cast_json" else k for k in body.keys()} & set(_EDIT_FIELD_MAP)
        if isinstance(body.get("meta"), dict):
            touched |= set(body["meta"].keys()) & set(EDIT_META_TEXT_KEYS)
        if has("wiki_no_doc"):
            touched.add("wiki_status")
        if has("air_date"):
            touched.add("broadcast_period")

        def _cmp(col, v):
            if col.endswith("_json"):
                try:
                    v = json.loads(v) if isinstance(v, str) else v
                except Exception:
                    pass
            return _norm_for_compare(v)

        def _parse_json_col(v):
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    return v
            return v

        changed: set[str] = set()
        for f in touched:
            fcols, mkeys = _EDIT_FIELD_MAP[f]
            if f == "title":
                if cols.get("title", old_title) != old_title:
                    changed.add(f)
            elif f in ("infobox", "wiki_sections"):
                # 컬럼이 정본. (통일 이전 형식으로 metadata_json 에만 남아 있던 값도 '원래 값'으로 봐야 오탐이 없다)
                col = fcols[0]
                old_eff = _parse_json_col(old_cols.get(col)) or old_meta.get(_LEGACY_META_KEY[col]) or None
                new_eff = (_parse_json_col(cols[col]) if col in cols else old_eff) or None
                if _norm_for_compare(old_eff) != _norm_for_compare(new_eff):
                    changed.add(f)
            elif f == "wiki_status":
                was_none = old_meta.get("wiki_status") == "none" or (old_desc == _NO_WIKI_DESC)
                if bool(body.get("wiki_no_doc")) != was_none:
                    changed.add(f)
            elif f == "broadcast_period":
                ob = old_meta.get("broadcast_period") if isinstance(old_meta.get("broadcast_period"), dict) else {}
                nb = meta.get("broadcast_period") if isinstance(meta.get("broadcast_period"), dict) else {}
                if (_cmp("air_date", cols.get("air_date", old_cols["air_date"])) != _cmp("air_date", old_cols["air_date"])
                        or _norm_for_compare(nb.get("end")) != _norm_for_compare(ob.get("end"))
                        or (ob.get("raw") and _norm_for_compare(nb.get("raw")) != _norm_for_compare(ob.get("raw")))):
                    changed.add(f)
            elif fcols:
                if any(c in cols and _cmp(c, cols[c]) != _cmp(c, old_cols[c]) for c in fcols):
                    changed.add(f)
            else:
                if any(_norm_for_compare(meta.get(k)) != _norm_for_compare(old_meta.get(k)) for k in mkeys):
                    changed.add(f)
        old_manual = manual_fields_of(old_meta)
        manual = (old_manual | changed) - release
        if origin_auto:
            manual.discard("origin")
            from services.origin import detect_origin
            meta.pop("origin", None)
            meta["origin"] = detect_origin(meta)

        auto_vals = dict(old_meta.get("auto_values") or {}) if isinstance(old_meta.get("auto_values"), dict) else {}

        # ── 수정 이력: '무엇이 A → B 로 바뀌었는지' 와 시각을 metadata_json.edit_log 에 누적 ──
        if changed:
            def _period_text(bp, fallback=""):
                if isinstance(bp, dict) and bp:
                    s, e = bp.get("start") or "", bp.get("end") or ""
                    return bp.get("raw") or (f"{s} ~ {e}" if s and e else (s or e)) or fallback
                return fallback

            def _old_new(f: str):
                fcols, mkeys = _EDIT_FIELD_MAP[f]
                if f == "title":
                    return old_title, cols.get("title", old_title)
                if f == "wiki_status":
                    was_none = old_meta.get("wiki_status") == "none" or (old_desc == _NO_WIKI_DESC)
                    return ("문서 없음" if was_none else "문서 있음"), ("문서 없음" if body.get("wiki_no_doc") else "문서 있음")
                if f == "broadcast_period":
                    return (_period_text(old_meta.get("broadcast_period"), old_cols.get("air_date") or ""),
                            _period_text(meta.get("broadcast_period"), cols.get("air_date") or ""))
                if f == "origin":
                    def _ot(o):
                        if not isinstance(o, dict) or not o.get("group") or o["group"] == "unknown":
                            return "미확인"
                        return ("국내" if o["group"] == "domestic" else "해외") + (f"({o['country']})" if o.get("country") else "")
                    return _ot(old_meta.get("origin")), _ot(meta.get("origin"))
                if f == "cast":
                    return _parse_json_col(old_cols.get("cast_json")), meta.get("cast")
                if f in ("infobox", "wiki_sections"):
                    col = fcols[0]
                    old_v = _parse_json_col(old_cols.get(col)) or old_meta.get(_LEGACY_META_KEY[col])
                    return old_v, (_parse_json_col(cols[col]) if col in cols else old_v)
                if fcols:
                    c = fcols[0]
                    return old_cols.get(c), cols.get(c, old_cols.get(c))
                return old_meta.get(mkeys[0]), meta.get(mkeys[0])

            def _fmt_val(f: str, v) -> str:
                if v is None or v == "" or v == [] or v == {}:
                    return ""
                if f == "infobox" and isinstance(v, dict):
                    return f"{len(v)}개 항목"
                if f == "wiki_sections" and isinstance(v, list):
                    return f"{len(v)}개 섹션"
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                s = str(v).strip()
                return s if len(s) <= 120 else s[:120] + "…"

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")   # UTC 로 저장 → 화면에서 로컬 시각으로 표시
            log = meta.get("edit_log") if isinstance(meta.get("edit_log"), list) else []
            for f in [k for k in _EDIT_FIELD_MAP if k in changed]:   # 항목 순서 고정
                o, n = _old_new(f)
                old_txt, new_txt = _fmt_val(f, o), _fmt_val(f, n)
                if f == "infobox" and isinstance(o, dict) and isinstance(n, dict):
                    keys = [k for k in dict.fromkeys([*o.keys(), *n.keys()]) if o.get(k) != n.get(k)]
                    new_txt += f" (변경: {', '.join(map(str, keys[:6]))}{' 외' if len(keys) > 6 else ''})" if keys else ""
                elif f == "wiki_sections" and old_txt == new_txt and old_txt:
                    new_txt += " (내용 수정)"
                entry = {"at": now_iso, "field": f, "label": _EDIT_LABELS.get(f, f),
                         "old": old_txt, "new": new_txt}
                if log_note:
                    entry["note"] = log_note
                # 되돌리기용 원본 값(body 형식). 너무 크면 생략 → 그 이력은 되돌릴 수 없다
                _ov = _field_body_value(f, old_cols, old_meta, old_title)
                _nv = _field_body_value(f, {**old_cols, **cols}, meta, cols.get("title", old_title))
                if _cap_raw(_ov) is not _MISSING and _cap_raw(_nv) is not _MISSING:
                    entry["ov"], entry["nv"] = _ov, _nv
                    # 처음으로 '직접 수정'되는 항목은 그 직전 값을 자동 값으로 보관한다
                    if f not in old_manual and f not in release and f not in auto_vals:
                        auto_vals[f] = _ov
                log.append(entry)
            meta["edit_log"] = log[-300:]

            # 여러 번 고쳐서 결국 '처음 값'으로 돌아온 항목은 수정된 것이 아니므로 수정됨 표시에서 뺀다
            # (긴 본문/구조 데이터는 이력 문자열로 비교할 수 없어 제외)
            for f in changed:
                if f in ("infobox", "wiki_sections"):
                    continue
                hist = [e for e in log if e.get("field") == f]
                if hist and hist[0]["old"] == hist[-1]["new"] and len(hist[0]["old"]) < 120 and len(hist[-1]["new"]) < 120:
                    manual.discard(f)

        if manual:
            meta["manual_fields"] = sorted(manual)
        else:
            meta.pop("manual_fields", None)
        auto_vals = {k: v for k, v in auto_vals.items() if k in manual}
        if auto_vals:
            meta["auto_values"] = auto_vals
        else:
            meta.pop("auto_values", None)

        cols["metadata_json"] = json.dumps(normalize_drama_meta(meta, cols.get("title", old_title)), ensure_ascii=False)
        sets = ", ".join(f"{k}=?" for k in cols)
        await db.execute(f"UPDATE dramas SET {sets} WHERE id=?", list(cols.values()) + [drama_id])
        if "title" in cols and cols["title"] != old_title:
            # 영상 레코드는 제목으로 작품과 연결되므로 함께 바꿔야 연결이 끊기지 않는다
            await db.execute("UPDATE youtube_videos SET drama_title=? WHERE drama_title=?",
                             (cols["title"], old_title))
        await sync_full_meta(db, drama_id)       # 인포박스·본문을 고쳤다면 metadata.wikipedia 도 같이 갱신
        await db.commit()
    return [k for k in body.keys()]


async def _load_drama_meta(drama_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT metadata_json FROM dramas WHERE id=?", (drama_id,))
        row = await cur.fetchone()
    if not row:
        raise LookupError(drama_id)
    try:
        meta = json.loads(row[0] or "{}") or {}
    except Exception:
        meta = {}
    return meta if isinstance(meta, dict) else {}


async def revert_drama_edit(drama_id: int, index: int, field: str) -> str:
    """수정 이력 index 번째 줄을 '수정하기 전 값'으로 되돌린다(새 이력 줄이 추가된다). 되돌린 항목명을 돌려준다."""
    meta = await _load_drama_meta(drama_id)
    log = meta.get("edit_log") if isinstance(meta.get("edit_log"), list) else []
    if not (isinstance(index, int) and 0 <= index < len(log)) or log[index].get("field") != field:
        raise ValueError("수정 이력을 찾을 수 없습니다. 화면을 새로고침한 뒤 다시 시도하세요.")
    entry = log[index]
    if field not in _EDIT_FIELD_MAP or "ov" not in entry:
        raise ValueError("이 이력에는 이전 값이 기록되어 있지 않아 되돌릴 수 없습니다.")
    await apply_drama_edit(drama_id, _body_from_field(field, entry["ov"]),
                           log_note="수정 이전 값으로 되돌림")
    return _EDIT_LABELS.get(field, field)


async def reset_drama_to_auto(drama_id: int, fields: list[str] | None = None) -> dict:
    """직접 수정한 항목을 '자동 값'(수집·위키가 채웠던 값)으로 되돌리고 수정됨 표시를 푼다.

    fields 가 None 이면 직접 수정된 항목 전부. 자동 값이 기록되지 않은 항목은 unavailable 로 알린다.
    자동 값 = ① 재수집 때 보관한 최신 수집값 → ② 처음 수정 직전 값(이력) → ③ 제작 국가는 자동 판별.
    """
    meta = await _load_drama_meta(drama_id)
    manual = manual_fields_of(meta)
    targets = sorted(manual if not fields else (manual & set(fields)))
    if not targets:
        return {"reset": [], "unavailable": []}
    av = meta.get("auto_values") if isinstance(meta.get("auto_values"), dict) else {}
    log = meta.get("edit_log") if isinstance(meta.get("edit_log"), list) else []
    body: dict = {}
    reset, unavailable = [], []
    for f in targets:
        if f == "origin":
            val = {"group": "auto", "country": ""}
        elif f in av:
            val = av[f]
        else:
            first = next((e for e in log if e.get("field") == f), None)   # 그 항목의 가장 오래된 수정 = 자동 값 상태
            if first is None or "ov" not in first:
                unavailable.append(_EDIT_LABELS.get(f, f))
                continue
            val = first["ov"]
        part = _body_from_field(f, val)
        if "meta" in part:
            body.setdefault("meta", {}).update(part["meta"])
        else:
            body.update(part)
        reset.append(f)
    if reset:
        await apply_drama_edit(drama_id, body, release=set(reset), log_note="자동 값으로 복원")
    return {"reset": [_EDIT_LABELS.get(f, f) for f in reset], "unavailable": unavailable}


async def get_dramas(
    q: str = "",
    channel: str = "",
    status: str = "",
    broadcaster: str = "",
    content_type: str = "",
    limit: int = 50,
    offset: int = 0,
    origin: str = "",
) -> tuple[list[dict], int]:
    filters = ["1=1"]
    params: list = []

    if q:
        filters.append("(title LIKE ? OR description LIKE ? OR cast_json LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    if channel:
        filters.append("LOWER(channel) LIKE LOWER(?)")
        params.append(f"%{channel}%")
    if status == "ok":
        # 공식사이트 보유: 공식 사이트 버튼이 뜨는(주소가 있는) 것
        filters.append(_HAS_OFFICIAL_SQL)
    elif status == "no_official":
        # 공식사이트 미보유: 주소가 없는 것
        filters.append(f"NOT {_HAS_OFFICIAL_SQL}")
    elif status == "no_wiki":
        # 위키피디아 문서 없음: wiki_status 가 'none' 이거나 설명이 '문서 없음' 안내문인 것
        filters.append(
            "((CASE WHEN metadata_json IS NOT NULL AND json_valid(metadata_json) "
            "THEN json_extract(metadata_json, '$.wiki_status') END) = 'none' OR description = ?)"
        )
        params.append(_NO_WIKI_DESC)
    elif status == "edited":
        # 사용자가 수동으로 수정한 항목 (metadata_json.manual_fields 가 비어있지 않은 것)
        filters.append(_EDITED_SQL)
    if broadcaster:
        filters.append("broadcaster LIKE ?")
        params.append(f"%{broadcaster}%")
    if content_type:
        filters.append("content_type = ?")
        params.append(content_type)
    if origin:
        # metadata_json.origin: domestic(국내) / foreign(해외) / unknown(판별 불가) / review(확인 필요 = unknown + 근거가 약한 것)
        _og = "(CASE WHEN metadata_json IS NOT NULL AND json_valid(metadata_json) THEN json_extract(metadata_json, '$.origin.group') END)"
        _oc = "(CASE WHEN metadata_json IS NOT NULL AND json_valid(metadata_json) THEN json_extract(metadata_json, '$.origin.confidence') END)"
        if origin in ("domestic", "foreign"):
            filters.append(f"{_og} = ?")
            params.append(origin)
        elif origin == "unknown":
            filters.append(f"({_og} IS NULL OR {_og} = 'unknown')")
        elif origin == "review":
            filters.append(f"({_og} IS NULL OR {_og} = 'unknown' OR {_oc} = 'low')")

    where = " AND ".join(filters)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM dramas WHERE {where}", params)
        drama_total = (await cur.fetchone())[0]

        cur = await db.execute(
            f"SELECT {_SELECT_COLS} FROM dramas WHERE {where} ORDER BY crawled_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        rows = await cur.fetchall()

    dramas = [_drama_from_row(r) for r in rows]

    # 모든 드라마에 youtube_videos에서 video_id/poster 연동 (poster 유무 무관)
    all_titles = [d["title"] for d in dramas if d.get("title")]
    if all_titles:
        async with aiosqlite.connect(DB_PATH) as db2:
            ph = ",".join("?" for _ in all_titles)
            cur2 = await db2.execute(
                f"SELECT drama_title, video_id, poster FROM youtube_videos"
                f" WHERE drama_title IN ({ph}) AND video_id NOT LIKE 'wiki\\_%' ESCAPE '\\'"
                f" GROUP BY drama_title",
                all_titles,
            )
            yt_map = {r[0]: {"video_id": r[1], "poster": r[2] or ""} for r in await cur2.fetchall()}
        for d in dramas:
            title = d.get("title")
            if title and title in yt_map:
                yt = yt_map[title]
                if not d.get("video_id"):
                    d["video_id"] = yt["video_id"]
                if not d.get("poster"):
                    _yt_p = yt.get("poster") or ""
                    # api.com2usn.com/files/... 이미지는 실제 드라마 포스터이므로 그대로 사용
                    d["poster"] = _yt_p

    return dramas, drama_total


async def get_drama_by_id(drama_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT {_SELECT_COLS} FROM dramas WHERE id=?", (drama_id,))
        row = await cur.fetchone()
    d = _drama_from_row(row) if row else None
    if d and d.get("title"):
        async with aiosqlite.connect(DB_PATH) as db2:
            cur2 = await db2.execute(
                "SELECT video_id, title, description, youtube_url, poster, source, category FROM youtube_videos"
                " WHERE drama_title=? AND video_id NOT LIKE 'wiki\\_%' ESCAPE '\\'"
                " ORDER BY crawled_at DESC",
                (d["title"],),
            )
            yt_rows = await cur2.fetchall()
        yt_keys = ["video_id", "title", "description", "youtube_url", "poster", "source", "category"]
        yt_list = [dict(zip(yt_keys, r)) for r in yt_rows]
        d["youtube_videos"] = yt_list
        if yt_list:
            if not d.get("video_id"):
                d["video_id"] = yt_list[0]["video_id"]
            if not d.get("poster"):
                _yt_poster = yt_list[0].get("poster") or ""
                # api.com2usn.com/files/... 이미지는 실제 드라마 포스터이므로 그대로 사용
                d["poster"] = _yt_poster
    return d


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM dramas")
        total = (await cur.fetchone())[0]
        # 공식사이트 보유: official_site_url이 비어있지 않은 것
        cur = await db.execute(f"SELECT COUNT(*) FROM dramas WHERE {_HAS_OFFICIAL_SQL}")
        ok = (await cur.fetchone())[0]
        # 공식사이트 미보유
        no_official = total - ok
        cur = await db.execute("SELECT COUNT(*) FROM crawl_queue WHERE status='pending'")
        pending = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM crawl_queue WHERE status='running'")
        running = (await cur.fetchone())[0]

    return {
        "total": total,
        "ok": ok,
        "no_official": no_official,
        "errors": 0,
        "queue_pending": pending,
        "queue_running": running,
    }


# ── 크롤 큐 관리 ─────────────────────────────────────────────────────────────

# ── 뉴스 기사 CRUD ────────────────────────────────────────────────────────────

import re as _re

def _normalize_news_url(url: str) -> str:
    """뉴스 기사 URL에서 page=, scode=, datepicker 등 목록 파라미터를 제거하여
    RECEIVE_DATE + SEQUENCE 기반 고유 URL로 정규화합니다."""
    if not url:
        return url
    m_date = _re.search(r'RECEIVE_DATE=(\d+)', url)
    m_seq  = _re.search(r'SEQUENCE=(\d+)', url)
    if m_date and m_seq:
        return (
            f"https://news.mbccb.co.kr/home/sub.php"
            f"?menukey=61&mod=view"
            f"&RECEIVE_DATE={m_date.group(1)}&SEQUENCE={m_seq.group(1)}"
        )
    return url


async def upsert_news_article(data: dict) -> tuple[int, bool]:
    """뉴스 기사 저장 (같은 URL이면 업데이트).
    반환: (art_id, is_new) — is_new=True이면 신규 INSERT, False이면 기존 UPDATE
    URL은 정규화(page= 등 제거)하여 저장합니다.
    """
    now = datetime.utcnow().isoformat()
    norm_url = _normalize_news_url(data.get("url", "")) or data.get("url", "")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id FROM news_articles WHERE url=?", (norm_url,))
        existing = await cur.fetchone()
        if existing:
            art_id = existing[0]
            # 새 이미지가 있으면 덮어씌움 (서버에서 이미지 파일 교체 대응), 없으면 기존 유지
            new_img = data.get("image_url", "")
            cur2 = await db.execute("SELECT image_url FROM news_articles WHERE id=?", (art_id,))
            old_row = await cur2.fetchone()
            old_img = old_row[0] if old_row else ""
            # 사용자가 수정한 필드는 재수집 결과로 덮어쓰지 않는다 (빈 값을 넘기면 아래 COALESCE 가 기존 값을 유지)
            cur3 = await db.execute("SELECT manual_fields, auto_values FROM news_articles WHERE id=?", (art_id,))
            _r3 = await cur3.fetchone() or [None, None]
            manual_news = _news_manual_set(_r3[0])
            if manual_news:
                # 버려지는 새 수집값은 '자동 값'으로 보관해 둔다 (나중에 '자동 값으로' 복원할 때 최신 수집값을 쓴다)
                _av = _news_json_dict(_r3[1])
                for _f in manual_news:
                    _nv = (new_img if _f == "image_url" else data.get(_f)) or ""
                    if str(_nv).strip() and len(str(_nv)) <= _NEWS_RAW_CAP:
                        _av[_f] = str(_nv).strip()
                await db.execute("UPDATE news_articles SET auto_values=? WHERE id=?",
                                 (json.dumps(_av, ensure_ascii=False), art_id))
                data = {**data, **{f: "" for f in manual_news if f != "image_url"}}
            # 새 이미지가 있고, 기존 이미지와 다른 경우 갱신 (URL 변경 대응)
            final_img = old_img if "image_url" in manual_news else (new_img if new_img else old_img)
            # 이미 유튜브 썸네일(영상 기사 대표 이미지)이 저장된 기사는, 영상 정보 없이 들어온 목록 썸네일로 되돌리지 않는다
            _yt_thumb = "img.youtube.com/" in (old_img or "")
            if _yt_thumb and "img.youtube.com/" not in (new_img or "") and not data.get("video_id"):
                final_img = old_img
            # 새 값이 비어 있으면 기존 값 유지 (이미지만 갱신할 때 본문/기자/날짜가 지워지는 것 방지)
            await db.execute("""
                UPDATE news_articles SET
                    title        = COALESCE(NULLIF(?, ''), title),
                    body         = COALESCE(NULLIF(?, ''), body),
                    reporter     = COALESCE(NULLIF(?, ''), reporter),
                    published_at = COALESCE(NULLIF(?, ''), published_at),
                    image_url    = ?,
                    video_id     = COALESCE(NULLIF(?, ''), video_id),
                    youtube_url  = COALESCE(NULLIF(?, ''), youtube_url),
                    crawled_at   = ?
                WHERE id=?
            """, (data.get("title",""), data.get("body",""),
                  data.get("reporter",""), data.get("published_at",""),
                  final_img, data.get("video_id",""), data.get("youtube_url",""),
                  now, art_id))
            await db.commit()
            return art_id, False
        else:
            try:
                cur = await db.execute("""
                    INSERT INTO news_articles (source, title, url, body, reporter, published_at, image_url, video_id, youtube_url, crawled_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (data.get("source","mbccb"), data.get("title",""), norm_url,
                      data.get("body",""), data.get("reporter",""),
                      data.get("published_at",""), data.get("image_url",""),
                      data.get("video_id",""), data.get("youtube_url",""), now))
                art_id = cur.lastrowid
                await db.commit()
                return art_id, True
            except Exception:
                # UNIQUE 제약 위반 시 (race condition) → 기존 행 반환
                cur = await db.execute("SELECT id FROM news_articles WHERE url=?", (norm_url,))
                row = await cur.fetchone()
                return (row[0] if row else -1), False


def _news_img_owner_ok(image_url: str, url: str) -> bool:
    """MBC충북 업로드 이미지 파일명(..._YYYYMMDD_SEQ.ext)이 이 기사(RECEIVE_DATE/SEQUENCE)의 것인지.
    파일명 패턴이 없는 이미지(유튜브 썸네일 등)는 판별 불가이므로 True."""
    m_t = _re.search(r'_(\d{8})_(\d+)\.', image_url or "")
    m_d = _re.search(r'RECEIVE_DATE=(\d{8})', url or "")
    m_s = _re.search(r'SEQUENCE=(\d+)', url or "")
    if not (m_t and m_d and m_s):
        return True
    return m_t.group(1) == m_d.group(1) and m_t.group(2).lstrip('0') == m_s.group(1).lstrip('0')


async def get_news_urls_to_recheck(source: str = "mbccb", days: int = 30) -> list[str]:
    """이미 저장됐지만 다시 확인해야 하는 기사 URL.
    - 영상이 기사 발행 뒤에 임베드되는 경우가 있어, 최근 `days`일 안의 기사 중 video_id 가 비어 있는 것
    - 이미지 파일명이 다른 기사 것인 것(옆 기사 이미지가 섞여 들어간 경우)"""
    cutoff = (datetime.utcnow() + timedelta(hours=9) - timedelta(days=days)).strftime("%Y%m%d")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT url, image_url, video_id, manual_fields FROM news_articles WHERE source=?", (source,)
        )
        rows = await cur.fetchall()
    out = []
    for url, img, vid, manual in rows:
        m_d = _re.search(r'RECEIVE_DATE=(\d{8})', url or "")
        recent_no_video = (not vid) and m_d and m_d.group(1) >= cutoff
        wrong_img = bool(img) and not _news_img_owner_ok(img, url) and "image_url" not in _news_manual_set(manual)
        if recent_no_video or wrong_img:
            out.append(_normalize_news_url(url) or url)
    return out


async def get_existing_news_urls(source: str = "mbccb") -> set:
    """DB에 이미 저장된 뉴스 기사 URL 집합 반환 (정규화된 URL로 반환, 중복 크롤링 방지용).
    이미지가 없는 기사는 제외해 재크롤링이 일어나도록 함."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT url FROM news_articles WHERE source=? AND image_url IS NOT NULL AND image_url != ''",
            (source,)
        )
        rows = await cur.fetchall()
    return {_normalize_news_url(r[0]) or r[0] for r in rows}


_NEWS_EDIT_LABELS = {
    "title": "제목", "body": "본문",
    "image_url": "이미지", "reporter": "기자", "published_at": "발행일",
}


def _news_json_list(v) -> list:
    if isinstance(v, list):
        return v
    try:
        r = json.loads(v or "[]")
        return r if isinstance(r, list) else []
    except Exception:
        return []


_NEWS_RAW_CAP = 50000   # 이력에 원본을 통째로 저장하는 최대 글자 수(기사 본문이 길어 드라마보다 크게 잡는다)


def _news_json_dict(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        r = json.loads(v or "{}")
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def _news_manual_set(v) -> set[str]:
    return {f for f in _news_json_list(v) if f in _NEWS_EDIT_LABELS}


async def _apply_simple_edit(table: str, row_id: int, labels: dict, body: dict, *,
                             required: tuple = (), long_fields: tuple = (),
                             keep_raw: bool = False, release: set[str] | None = None, note: str = "") -> list[str]:
    """뉴스/영상처럼 '평평한 텍스트 컬럼' 레코드의 수정 공통 로직.
    값 반영 + 실제로 바뀐 필드만 manual_fields(재수집 보호)와 edit_log(이력)에 기록.
    반환: 실제로 값이 바뀐 필드 목록. 행이 없으면 LookupError, 필수 필드가 비면 ValueError."""
    import hashlib
    fields = list(labels)
    release = set(release or ())
    touched = [f for f in fields if f in body]
    if not touched and not release:
        raise ValueError("수정할 필드가 없습니다.")
    new_vals = {f: ("" if body[f] is None else str(body[f]).strip()) for f in touched}
    for f in required:
        if f in new_vals and not new_vals[f]:
            raise ValueError(f"{labels[f]}은(는) 비울 수 없습니다.")

    async with aiosqlite.connect(DB_PATH) as db:
        extra_col = ", auto_values" if keep_raw else ""
        cur = await db.execute(
            f"SELECT {', '.join(fields)}, manual_fields, edit_log{extra_col} FROM {table} WHERE id=?", (row_id,))
        row = await cur.fetchone()
        if not row:
            raise LookupError("항목을 찾을 수 없습니다.")
        old = {f: (row[i] or "") for i, f in enumerate(fields)}
        manual = {f for f in _news_json_list(row[len(fields)]) if f in labels}
        log = _news_json_list(row[len(fields) + 1])
        auto_vals = _news_json_dict(row[len(fields) + 2]) if keep_raw else {}
        old_manual = set(manual)

        changed = [f for f in touched if new_vals[f] != old[f].strip()]
        if not changed and not release:
            return []

        def _h(v: str) -> str:
            return hashlib.md5(v.encode("utf-8")).hexdigest()[:10]

        def _fmt(f: str, v: str) -> str:
            if f in long_fields:
                return f"{labels[f]} {len(v)}자" if v else ""
            return v if len(v) <= 120 else v[:120] + "…"

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for f in changed:
            o, n = old[f].strip(), new_vals[f]
            entry = {"at": now_iso, "field": f, "label": labels[f],
                     "old": _fmt(f, o), "new": _fmt(f, n), "oh": _h(o), "nh": _h(n)}
            if note:
                entry["note"] = note
            if keep_raw and len(o) <= _NEWS_RAW_CAP and len(n) <= _NEWS_RAW_CAP:
                entry["ov"], entry["nv"] = o, n            # 되돌리기용 원본 (너무 길면 생략 → 되돌리기 불가)
                if f not in old_manual and f not in release and f not in auto_vals:
                    auto_vals[f] = o                        # 처음 수정되는 항목은 그 직전 값을 자동 값으로 보관
            log.append(entry)
        log = log[-300:]
        manual |= set(changed)
        manual -= release
        # 여러 번 고쳐서 처음 값으로 돌아온 필드는 '수정됨'에서 제외
        for f in list(manual):
            hist = [e for e in log if e.get("field") == f and "oh" in e]
            if hist and hist[0]["oh"] == hist[-1]["nh"]:
                manual.discard(f)

        sets = "".join(f"{f}=?, " for f in changed)
        auto_sql, auto_param = "", []
        if keep_raw:
            auto_vals = {k: v for k, v in auto_vals.items() if k in manual}
            auto_sql, auto_param = ", auto_values=?", [json.dumps(auto_vals, ensure_ascii=False)]
        await db.execute(
            f"UPDATE {table} SET {sets}manual_fields=?, edit_log=?{auto_sql} WHERE id=?",
            [new_vals[f] for f in changed]
            + [json.dumps(sorted(manual), ensure_ascii=False), json.dumps(log, ensure_ascii=False)] + auto_param + [row_id])
        await db.commit()
    return changed


async def apply_news_edit(article_id: int, body: dict) -> list[str]:
    """뉴스 기사 수정 (제목/요약/본문/이미지/기자/발행일). 제목은 비울 수 없다."""
    try:
        return await _apply_simple_edit("news_articles", article_id, _NEWS_EDIT_LABELS, body,
                                        required=("title",), long_fields=("body",), keep_raw=True)
    except LookupError:
        raise LookupError("기사를 찾을 수 없습니다.")


async def _load_news_edit_state(article_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT manual_fields, edit_log, auto_values FROM news_articles WHERE id=?", (article_id,))
        row = await cur.fetchone()
    if not row:
        raise LookupError("기사를 찾을 수 없습니다.")
    return _news_manual_set(row[0]), _news_json_list(row[1]), _news_json_dict(row[2])


async def revert_news_edit(article_id: int, index: int, field: str) -> str:
    """뉴스 수정 이력 index 번째 줄을 '수정하기 전 값'으로 되돌린다(새 이력 줄이 추가된다)."""
    _, log, _av = await _load_news_edit_state(article_id)
    if not (isinstance(index, int) and 0 <= index < len(log)) or log[index].get("field") != field:
        raise ValueError("수정 이력을 찾을 수 없습니다. 화면을 새로고침한 뒤 다시 시도하세요.")
    entry = log[index]
    if field not in _NEWS_EDIT_LABELS or "ov" not in entry:
        raise ValueError("이 이력에는 이전 값이 기록되어 있지 않아 되돌릴 수 없습니다.")
    await apply_news_edit_with(article_id, {field: entry["ov"]}, note="수정 이전 값으로 되돌림")
    return _NEWS_EDIT_LABELS[field]


async def apply_news_edit_with(article_id: int, body: dict, *, release: set[str] | None = None, note: str = "") -> list[str]:
    try:
        return await _apply_simple_edit("news_articles", article_id, _NEWS_EDIT_LABELS, body,
                                        required=("title",), long_fields=("body",), keep_raw=True,
                                        release=release, note=note)
    except LookupError:
        raise LookupError("기사를 찾을 수 없습니다.")


async def reset_news_to_auto(article_id: int, fields: list[str] | None = None) -> dict:
    """직접 수정한 뉴스 항목을 자동 값(수집이 채웠던 값)으로 되돌리고 수정됨 표시를 푼다.
    자동 값 = ① 재수집 때 보관한 최신 수집값 → ② 처음 수정 직전 값(이력). 둘 다 없으면 unavailable."""
    manual, log, av = await _load_news_edit_state(article_id)
    targets = sorted(manual if not fields else (manual & set(fields)))
    body, reset, unavailable = {}, [], []
    for f in targets:
        if f in av:
            val = av[f]
        else:
            first = next((e for e in log if e.get("field") == f), None)   # 그 항목의 가장 오래된 수정 = 자동 값 상태
            if first is None or "ov" not in first:
                unavailable.append(_NEWS_EDIT_LABELS[f])
                continue
            val = first["ov"]
        body[f] = val
        reset.append(f)
    if reset:
        await apply_news_edit_with(article_id, body, release=set(reset), note="자동 값으로 복원")
    return {"reset": [_NEWS_EDIT_LABELS[f] for f in reset], "unavailable": unavailable}


_VIDEO_EDIT_LABELS = {"title": "제목", "drama_title": "목록 제목", "description": "설명"}


async def apply_video_edit(row_id: int, body: dict) -> list[str]:
    """YouTube 영상(다시보기/LIVE) 수정 (제목/목록 제목/설명). 제목은 비울 수 없다.
    공통 형식의 키(list_title, body)도 받는다."""
    body = {_NEWS_TO_VIDEO_FIELD.get(k, k): v for k, v in body.items()}
    try:
        return await _apply_simple_edit("youtube_videos", row_id, _VIDEO_EDIT_LABELS, body,
                                        required=("title",), long_fields=("description",))
    except LookupError:
        raise LookupError("영상을 찾을 수 없습니다.")


_NEWS_EDITED_SQL = "(manual_fields IS NOT NULL AND manual_fields NOT IN ('', '[]', 'null'))"

# 뉴스·다시보기(vod)·뉴스LIVE(live) 공통 항목 형식.
# 종류에 상관없이 API 는 항상 이 키를 이 순서로 내보낸다(없는 값은 null).
NEWS_ITEM_KEYS = (
    "type", "id", "source", "title", "body", "reporter", "published_at", "crawled_at",
    "url", "image_url", "video_id", "youtube_url", "list_title", "manual_fields", "edit_log",
)
# 영상 컬럼 → 공통 키 (DB 컬럼 이름은 그대로 두고 API 경계에서만 바꾼다)
_VIDEO_TO_NEWS_FIELD = {"description": "body", "drama_title": "list_title"}
_NEWS_TO_VIDEO_FIELD = {v: k for k, v in _VIDEO_TO_NEWS_FIELD.items()}


def to_news_item(row: dict, typ: str) -> dict:
    """DB 행 한 건 → 공통 형식 항목. typ: 'news'(기사) | 'vod'(다시보기) | 'live'(뉴스LIVE)

    - 컬럼 이름이 다른 값은 같은 키로 모은다
      body(기사 본문 / 영상 설명), url(기사 주소 / 영상 page_url), image_url(기사 이미지 / 영상 poster),
      list_title(영상 목록 제목)
    - 그 종류에 없는 값(예: 영상의 reporter·published_at)과 빈 문자열은 null
    - manual_fields / edit_log 는 항상 리스트이고, 영상의 필드명도 공통 키(body, list_title)로 바꿔 내보낸다
    """
    is_video = typ in ("vod", "live")
    g = row.get

    def nz(v):
        return None if v is None or (isinstance(v, str) and not v.strip()) else v

    raw = {
        "type": typ, "id": g("id"), "source": g("source"), "title": g("title"),
        "body": g("description") if is_video else g("body"),
        "reporter": None if is_video else g("reporter"),
        "published_at": None if is_video else g("published_at"),
        "crawled_at": g("crawled_at"),
        "url": g("page_url") if is_video else g("url"),
        "image_url": g("poster") if is_video else g("image_url"),
        "video_id": g("video_id"), "youtube_url": g("youtube_url"),
        "list_title": g("drama_title") if is_video else None,
    }
    item = {k: nz(raw[k]) for k in NEWS_ITEM_KEYS if k in raw}

    def rename(f):
        return _VIDEO_TO_NEWS_FIELD.get(f, f) if is_video else f

    item["manual_fields"] = sorted({rename(f) for f in (g("manual_fields") or [])})
    item["edit_log"] = [
        {**e, "field": rename(e.get("field")), **({"label": "본문"} if is_video and e.get("field") == "description" else {})}
        for e in (g("edit_log") or [])
    ]
    return item


async def get_news_articles(q: str = "", source: str = "", limit: int = 20, offset: int = 0, edited: bool = False):
    filters, params = ["1=1"], []
    if q:
        filters.append("(title LIKE ? OR body LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if source:
        filters.append("source=?")
        params.append(source)
    if edited:
        filters.append(_NEWS_EDITED_SQL)
    where = " AND ".join(filters)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM news_articles WHERE {where}", params)
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            f"SELECT id,source,title,url,body,reporter,published_at,image_url,video_id,youtube_url,crawled_at,"
            f"manual_fields,edit_log "
            f"FROM news_articles WHERE {where} ORDER BY published_at DESC, crawled_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        )
        rows = await cur.fetchall()
    keys = ["id","source","title","url","body","reporter","published_at","image_url","video_id","youtube_url","crawled_at",
            "manual_fields","edit_log"]
    items = []
    for r in rows:
        d = dict(zip(keys, r))
        d["manual_fields"] = sorted(_news_manual_set(d["manual_fields"]))
        # 원본 값(ov/nv)은 목록을 무겁게 하므로 내보내지 않고, 되돌릴 수 있는 줄인지(rv)만 알려 준다
        d["edit_log"] = [{**{k: v for k, v in e.items() if k not in ("oh", "nh", "ov", "nv")}, "rv": "ov" in e}
                         for e in _news_json_list(d["edit_log"])]
        items.append(to_news_item(d, "news"))
    return items, total


# ── 유튜브 영상 CRUD ──────────────────────────────────────────────────────────

async def upsert_youtube_video(data: dict) -> tuple[int, bool]:
    """유튜브 영상 저장 (video_id 기준 upsert).
    반환: (vid_id, is_new) — is_new=True이면 신규 INSERT, False이면 기존 UPDATE
    """
    now = datetime.utcnow().isoformat()
    wiki_data = data.get("wiki_data")
    if isinstance(wiki_data, dict):
        wiki_data = json.dumps(wiki_data, ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as db:
        # wiki_data 컬럼 마이그레이션
        try:
            await db.execute("ALTER TABLE youtube_videos ADD COLUMN wiki_data TEXT")
            await db.commit()
        except Exception:
            pass
        cur = await db.execute("SELECT id FROM youtube_videos WHERE video_id=?", (data.get("video_id",""),))
        existing = await cur.fetchone()
        if existing:
            vid_id = existing[0]
            # 사용자가 수정한 필드(제목/목록 제목/설명)는 재수집 결과로 덮어쓰지 않는다
            cur_m = await db.execute("SELECT manual_fields, title, description, drama_title FROM youtube_videos WHERE id=?", (vid_id,))
            mrow = await cur_m.fetchone() or (None, "", "", "")
            manual_v = {f for f in _news_json_list(mrow[0]) if f in _VIDEO_EDIT_LABELS}
            data = {**data, **{f: mrow[{"title": 1, "description": 2, "drama_title": 3}[f]] or "" for f in manual_v}}
            await db.execute("""
                UPDATE youtube_videos SET source=?, category=?, title=?, description=?,
                    youtube_url=?, page_url=?, drama_title=?, poster=?, wiki_data=?, crawled_at=? WHERE id=?
            """, (data.get("source",""), data.get("category",""), data.get("title",""),
                  data.get("description",""), data.get("youtube_url",""), data.get("page_url",""),
                  data.get("drama_title",""), data.get("poster",""), wiki_data, now, vid_id))
            await db.commit()
            return vid_id, False
        else:
            cur = await db.execute("""
                INSERT INTO youtube_videos (source, category, video_id, title, description,
                    youtube_url, page_url, drama_title, poster, wiki_data, crawled_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (data.get("source",""), data.get("category",""), data.get("video_id",""),
                  data.get("title",""), data.get("description",""), data.get("youtube_url",""),
                  data.get("page_url",""), data.get("drama_title",""), data.get("poster",""), wiki_data, now))
            vid_id = cur.lastrowid
            await db.commit()
            return vid_id, True


async def get_existing_youtube_video_ids(source: str = "") -> set:
    """DB에 이미 저장된 YouTube video_id 집합 반환 (중복 크롤링 방지용)."""
    async with aiosqlite.connect(DB_PATH) as db:
        if source:
            cur = await db.execute("SELECT video_id FROM youtube_videos WHERE source=?", (source,))
        else:
            cur = await db.execute("SELECT video_id FROM youtube_videos")
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def get_existing_youtube_page_urls(source: str, category: str = "") -> set:
    """이미 수집 완료된 영상의 page_url 집합 (상세 페이지 재방문 방지용).
    YouTube 를 못 찾아 만든 대체 항목(video_id 가 'wiki_' 로 시작)은 재시도 대상이므로 제외한다."""
    sql = ("SELECT page_url FROM youtube_videos WHERE source=? AND page_url IS NOT NULL AND page_url != '' "
           "AND video_id NOT LIKE 'wiki\\_%' ESCAPE '\\'")
    params: list = [source]
    if category:
        sql += " AND category=?"
        params.append(category)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    return {r[0] for r in rows}


async def get_youtube_videos(source: str = "", category: str = "", q: str = "",
                             limit: int = 20, offset: int = 0, edited: bool = False, row_id: int = 0):
    filters, params = ["1=1"], []
    if row_id:
        filters.append("id=?"); params.append(row_id)
    if edited:
        filters.append(_NEWS_EDITED_SQL)
    if source:
        filters.append("source=?"); params.append(source)
    if category:
        filters.append("category=?"); params.append(category)
    if q:
        filters.append("(title LIKE ? OR description LIKE ? OR drama_title LIKE ?)")
        params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    where = " AND ".join(filters)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM youtube_videos WHERE {where}", params)
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            f"SELECT id,source,category,video_id,title,description,youtube_url,page_url,drama_title,poster,wiki_data,crawled_at,"
            f"manual_fields,edit_log "
            f"FROM youtube_videos WHERE {where} ORDER BY crawled_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset]
        )
        rows = await cur.fetchall()
    keys = ["id","source","category","video_id","title","description","youtube_url","page_url","drama_title","poster","wiki_data","crawled_at",
            "manual_fields","edit_log"]
    result = []
    for r in rows:
        item = dict(zip(keys, r))
        item["manual_fields"] = sorted(f for f in _news_json_list(item["manual_fields"]) if f in _VIDEO_EDIT_LABELS)
        item["edit_log"] = [{k: v for k, v in e.items() if k not in ("oh", "nh")} for e in _news_json_list(item["edit_log"])]
        if item.get("wiki_data"):
            try:
                item["wiki_data"] = json.loads(item["wiki_data"])
            except Exception:
                pass
        if item.get("source") == "mbccb" and item.get("category") in ("vod", "live"):
            item = to_news_item(item, item["category"])      # 뉴스와 같은 공통 형식으로 내보낸다
        result.append(item)
    return result, total


async def get_mbccb_all(q: str = "", limit: int = 20, offset: int = 0, edited: bool = False) -> dict:
    """MBC충북 '전체보기': 뉴스 → 다시보기 → LIVE 순서로 이어 붙인 하나의 목록을 페이지 단위로 반환.
    모든 항목이 같은 공통 형식이며 type('news'|'vod'|'live')으로 종류를 구분한다. 종류별 총 개수(counts)도 함께 돌려준다."""
    _, n_tot = await get_news_articles(q=q, source="mbccb", limit=1, offset=0, edited=edited)
    _, v_tot = await get_youtube_videos(source="mbccb", category="vod", q=q, limit=1, edited=edited)
    _, l_tot = await get_youtube_videos(source="mbccb", category="live", q=q, limit=1, edited=edited)

    items: list[dict] = []
    remaining, cur = limit, offset
    for typ, tot in (("news", n_tot), ("vod", v_tot), ("live", l_tot)):
        if remaining <= 0:
            break
        if cur >= tot:           # 이 구간은 이전 페이지에서 모두 지나감
            cur -= tot
            continue
        take = min(remaining, tot - cur)
        if typ == "news":
            rows, _ = await get_news_articles(q=q, source="mbccb", limit=take, offset=cur, edited=edited)
        else:
            rows, _ = await get_youtube_videos(source="mbccb", category=typ, q=q, limit=take, offset=cur, edited=edited)
        items += rows
        remaining -= take
        cur = 0
    return {"total": n_tot + v_tot + l_tot, "offset": offset, "limit": limit, "items": items,
            "counts": {"news": n_tot, "vod": v_tot, "live": l_tot}}
