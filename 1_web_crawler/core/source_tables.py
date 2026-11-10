"""출처별 원본·인물·회차 테이블 (dramas 테이블은 그대로 두고 옆에 붙인다).

dramas            = 작품 1건의 정본(화면에 보이는 값). 필드별 우선순위로 병합된 결과.
drama_sources     = 출처(tvn/tving/wiki/…)별 원본 레코드. 병합 근거와 매칭 방법·확신도를 함께 기록.
drama_people      = 인물. kind='character'(배역, tvN 드라마) | 'cast'(출연자 실명).
                    짧은 설명(brief)·긴 설명(detail)을 분리하고, 배역의 실제 배우는 actor_name 에 따로 채운다.
drama_episodes    = 회차별 요약. (drama_id, ep_no, source) 로 출처별로 따로 보관 (TVING/tvN 이 같은 회차를 각자 가질 수 있음)
"""
from __future__ import annotations

import json
from datetime import datetime

DDL = [
    """CREATE TABLE IF NOT EXISTS drama_sources (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        drama_id      INTEGER NOT NULL,
        source        TEXT NOT NULL,           -- tvn | tving | wiki | com2usn
        source_key    TEXT NOT NULL,           -- tvN pgmId(B…), TVING 코드(P…), 위키 URL …
        url           TEXT,
        record_json   TEXT,                    -- 해당 출처에서 정규화한 레코드 전체
        match_method  TEXT,                    -- tving_code | title+channel+year | manual | origin
        confidence    REAL,
        fetched_at    TEXT,
        UNIQUE(source, source_key)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_drama_sources_drama ON drama_sources(drama_id)",
    """CREATE TABLE IF NOT EXISTS drama_people (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        drama_id      INTEGER NOT NULL,
        source        TEXT NOT NULL,
        kind          TEXT NOT NULL,           -- character | cast
        group_name    TEXT,
        name          TEXT NOT NULL,
        brief         TEXT,                    -- 짧은 설명 (예: '32세 / 태강그룹 차기 후계자 · 살인사건 용의자')
        detail        TEXT,                    -- 긴 설명
        image         TEXT,
        ord           INTEGER,
        actor_name    TEXT,                    -- character 일 때 실제 배우 (위키/TVING 에서 연결)
        actor_source  TEXT,
        UNIQUE(drama_id, source, kind, name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_drama_people_drama ON drama_people(drama_id)",
    """CREATE TABLE IF NOT EXISTS drama_episodes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        drama_id      INTEGER NOT NULL,
        ep_no         INTEGER,                 -- 번호를 알 수 없는 항목(스페셜 등)은 NULL
        source        TEXT NOT NULL,
        title         TEXT,
        tagline       TEXT,
        synopsis      TEXT,
        posted_at     TEXT,
        UNIQUE(drama_id, source, title)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_drama_episodes_drama ON drama_episodes(drama_id)",
]


async def ensure_tables(db) -> None:
    for stmt in DDL:
        await db.execute(stmt)


async def save_source_record(db, drama_id: int, rec: dict, *, method: str, confidence: float) -> dict:
    """출처 레코드(rec['source'] = tvn | tving)를 원본·인물·회차 테이블에 저장한다 (재실행해도 같은 키는 갱신)."""
    src = rec["source"]
    now = datetime.utcnow().isoformat()
    await db.execute(
        """INSERT INTO drama_sources (drama_id, source, source_key, url, record_json, match_method, confidence, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(source, source_key) DO UPDATE SET
             drama_id=excluded.drama_id, url=excluded.url, record_json=excluded.record_json,
             match_method=excluded.match_method, confidence=excluded.confidence, fetched_at=excluded.fetched_at""",
        (drama_id, src, rec["source_key"], rec.get("url"), json.dumps(rec, ensure_ascii=False), method, confidence, now),
    )
    n_people = n_eps = 0
    for p in rec.get("people") or []:
        await db.execute(
            """INSERT INTO drama_people (drama_id, source, kind, group_name, name, brief, detail, image, ord)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(drama_id, source, kind, name) DO UPDATE SET
                 group_name=excluded.group_name, brief=excluded.brief, detail=excluded.detail,
                 image=excluded.image, ord=excluded.ord""",
            (drama_id, src, p["kind"], p["group"], p["name"], p["brief"], p["detail"], p["image"], p["order"]),
        )
        n_people += 1
    for e in rec.get("episodes") or []:
        await db.execute(
            """INSERT INTO drama_episodes (drama_id, ep_no, source, title, tagline, synopsis, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(drama_id, source, title) DO UPDATE SET
                 ep_no=excluded.ep_no, tagline=excluded.tagline, synopsis=excluded.synopsis, posted_at=excluded.posted_at""",
            (drama_id, e["no"], src, e["title"], e["tagline"], e["synopsis"], e["posted_at"]),
        )
        n_eps += 1
    return {"people": n_people, "episodes": n_eps}


async def save_tvn_record(db, drama_id: int, rec: dict, *, method: str, confidence: float) -> dict:
    return await save_source_record(db, drama_id, rec, method=method, confidence=confidence)


async def save_tving_record(db, drama_id: int, rec: dict, *, method: str, confidence: float) -> dict:
    return await save_source_record(db, drama_id, rec, method=method, confidence=confidence)


async def find_drama_id_by_source(db, source: str, source_key: str) -> int | None:
    cur = await db.execute("SELECT drama_id FROM drama_sources WHERE source=? AND source_key=?", (source, source_key))
    row = await cur.fetchone()
    return row[0] if row else None


async def delete_for_dramas(db, drama_ids: list[int]) -> None:
    """작품 삭제 시 옆 테이블 행도 같이 지운다 (테이블이 아직 없으면 무시)."""
    if not drama_ids:
        return
    ph = ",".join("?" for _ in drama_ids)
    for t in ("drama_sources", "drama_people", "drama_episodes"):
        try:
            await db.execute(f"DELETE FROM {t} WHERE drama_id IN ({ph})", list(drama_ids))
        except Exception:
            pass


async def get_extras(drama_id: int) -> dict:
    """화면용: 출처 목록 · 인물 · 회차 (출처가 없으면 빈 구조)."""
    import aiosqlite
    from core import database
    out = {"sources": [], "people": [], "episodes": []}
    async with aiosqlite.connect(database.DB_PATH) as db:
        try:
            cur = await db.execute(
                "SELECT source, source_key, url, match_method, confidence, fetched_at, record_json FROM drama_sources "
                "WHERE drama_id=? ORDER BY id", (drama_id,))
            for src, key, url, how, conf, at, rj in await cur.fetchall():
                try:
                    rec = json.loads(rj) if rj else {}
                except Exception:
                    rec = {}
                # 화면의 '기본정보 · 기획의도 · 프로그램 소개'용 요약 (인물·회차 같은 큰 목록은 제외)
                info = {k: rec.get(k) for k in ("kind", "sub_genre", "genre", "status", "air_time_text", "channel",
                                                "intent_short", "intro_long", "synopsis") if rec.get(k)}
                out["sources"].append({"source": src, "source_key": key, "url": url, "match_method": how,
                                       "confidence": conf, "fetched_at": at, "info": info})
            # 위키피디아 문서를 가져온 작품이면 출처에 위키피디아도 함께 보여준다
            cur = await db.execute(
                "SELECT wiki_url, wiki_sections_json IS NOT NULL AND wiki_sections_json NOT IN ('', '[]') "
                "FROM dramas WHERE id=?", (drama_id,))
            wrow = await cur.fetchone()
            if wrow and (wrow[0] or "").startswith("http") and not any(x["source"] == "wiki" for x in out["sources"]):
                out["sources"].append({"source": "wiki", "source_key": None, "url": wrow[0],
                                       "match_method": "위키 본문·등장인물" if wrow[1] else "문서 링크",
                                       "confidence": None, "fetched_at": None, "info": {}})
            cur = await db.execute(
                "SELECT source, kind, group_name, name, brief, detail, image, ord, actor_name, actor_source "
                "FROM drama_people WHERE drama_id=? ORDER BY source, kind, ord, id", (drama_id,))
            out["people"] = [dict(zip(("source", "kind", "group", "name", "brief", "detail", "image", "order",
                                       "actor_name", "actor_source"), r)) for r in await cur.fetchall()]
            cur = await db.execute(
                "SELECT source, ep_no, title, tagline, synopsis, posted_at FROM drama_episodes "
                "WHERE drama_id=? ORDER BY (ep_no IS NULL), ep_no, source, id", (drama_id,))
            out["episodes"] = [dict(zip(("source", "no", "title", "tagline", "synopsis", "posted_at"), r))
                               for r in await cur.fetchall()]
        except Exception:      # 테이블이 아직 만들어지지 않은 DB
            pass
    return out


def _kor_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{y}년 {int(m)}월 {int(d)}일"


def official_period(records: dict[str, dict]) -> dict | None:
    """공식 출처(위키에 기간이 없을 때의 대체값)의 방영 기간. 우선순위 TVING → tvN. 둘 다 날짜가 없으면 None.
    records: {'tving': 레코드, 'tvn': 레코드}.  반환: {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'|'', 'raw': '…'}"""
    import re
    tv = records.get("tving") or {}
    if tv.get("start_date"):
        start, end = tv["start_date"], tv.get("end_date") or ""
    else:
        dates = re.findall(r"(\d{4})\.(\d{2})\.(\d{2})", (records.get("tvn") or {}).get("air_time_text") or "")
        if dates:
            start = "-".join(dates[0])
            end = "-".join(dates[1]) if len(dates) > 1 else ""
        else:
            # 방송예정작: '11/26 [목] 저녁 8:40 첫 방송' — 연도는 수집 시각 기준(이미 지난 날짜면 다음 해)
            tvn = records.get("tvn") or {}
            m = re.search(r"(\d{1,2})/(\d{1,2})\s*\[[^\]]*\].*첫\s*방송", tvn.get("air_time_text") or "")
            ym = re.match(r"(\d{4})-(\d{2})-(\d{2})", tvn.get("collected_at") or "")
            if not (m and ym):
                return None
            from datetime import date
            y0 = int(ym.group(1)); mo, d = int(m.group(1)), int(m.group(2))
            try:
                cand = date(y0, mo, d)
                if (date(y0, int(ym.group(2)), int(ym.group(3))) - cand).days > 60:
                    cand = date(y0 + 1, mo, d)
            except ValueError:
                return None
            start, end = cand.isoformat(), ""
    raw = _kor_date(start) + (f" ~ {_kor_date(end)}" if end else " ~")
    return {"start": start, "end": end, "raw": raw}


def wiki_period(title: str, infobox: dict | None) -> dict | None:
    """위키 인포박스 '방송 기간'에서 이 행(작품·시즌)의 방영 기간을 뽑는다. 믿을 수 없으면 None.

    - 시즌 2 이상 행: 위키에 시즌별 기간이 이어 적혀 있을 때만 해당 시즌 기간(못 고르면 None → 공식 출처 값 사용)
    - 시즌 1·시즌 미표기 행: 기간이 여러 개면 첫 시작 ~ 마지막 끝(마지막이 '현재'거나 끝이 없으면 방영 중)
    반환: {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'|'', 'raw': '…'}"""
    import re
    from services.work_matcher import split_season, season_period, _KRANGE_RE
    ib = infobox or {}
    raw = ib.get("방송 기간") or ib.get("방영 기간") or ""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = re.sub(r"\s+", " ", raw)
    season = split_season(title or "")[1]
    if season and season >= 2:
        return season_period(text, season)
    if season == 1:
        sp = season_period(text, 1, first_ok=True)
        if sp:
            return sp
    ranges = []                      # (시작 튜플, 끝 튜플|None, 현재 여부, 시작 위치)
    for m in _KRANGE_RE.finditer(text):
        g = m.groups()
        ranges.append((tuple(int(x) for x in g[0:3]), tuple(int(x) for x in g[3:6]) if g[3] else None,
                       "현재" in m.group(0), m.start()))
    if not ranges:
        return None
    pick = ranges[0]                 # 기본: 첫 기간(원 방영분). 재방·해외·플랫폼별 기간이 뒤에 붙는 경우가 많다
    for r in ranges:                 # '대만 : … 한국 : …' 처럼 국가 라벨이 있으면 한국 기간
        if re.search(r"한국\s*:?\s*$", text[max(0, r[3] - 8):r[3]]):
            pick = r
            break
    start, end, ongoing = pick[0], pick[1], pick[2]
    if len(ranges) > 1 and pick is ranges[0]:
        last = ranges[-1]
        if re.search(r"(?:\d+\s*기|시즌\s*\d+)\s*:", text):      # '1기 : … 4기 : … ~ 현재', '시즌 1 : … 시즌 2 : …' 처럼 시즌이 이어진 예능
            end, ongoing = last[1], last[2]
        elif not any(r[1] or r[2] for r in ranges) and "파트" in text:
            end = last[0]            # '2024년 8월 29일 (파트1) 2024년 9월 12일 (파트2)'
    iso = lambda t: f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d}"
    kd = lambda t: f"{t[0]}년 {t[1]}월 {t[2]}일"
    try:
        from datetime import date
        date(*start)
        if end:
            date(*end)
    except ValueError:
        return None
    ongoing = (not end) and ongoing
    return {"start": iso(start), "end": iso(end) if end else "",
            "raw": kd(start) + (f" ~ {kd(end)}" if end else " ~ 현재" if ongoing else " ~")}


async def apply_official_period(db, drama_id: int) -> bool:
    """방영일 · metadata.broadcast_period · air_date 를 맞춘다. 우선순위: 위키피디아 → TVING → tvN.

    위키 인포박스에 이 작품(시즌)의 방영 기간이 있으면 그 값을 쓰고(TVING·tvN 과 달라도 위키 기준),
    위키에 없거나 시즌을 가려낼 수 없을 때만 TVING → tvN 값으로 채운다. 사용자가 직접 수정한 값은 유지한다.
    값이 바뀌었으면 True."""
    from core.database import manual_fields_of
    cur = await db.execute("SELECT title, infobox_json, metadata_json, air_date FROM dramas WHERE id=?", (drama_id,))
    row = await cur.fetchone()
    if not row:
        return False
    title, ib_json, meta_json, cur_air = row
    try:
        meta = json.loads(meta_json or "{}") or {}
    except Exception:
        meta = {}
    manual = manual_fields_of(meta)
    if "broadcast_period" in manual or "air_date" in manual:
        return False
    try:
        infobox = json.loads(ib_json or "{}") or {}
    except Exception:
        infobox = {}
    period = wiki_period(title or "", infobox if isinstance(infobox, dict) else {})
    if not period:
        records = {}
        try:
            cur = await db.execute("SELECT source, record_json FROM drama_sources WHERE drama_id=? AND source IN ('tving','tvn')", (drama_id,))
            for src, rj in await cur.fetchall():
                try:
                    records[src] = json.loads(rj or "{}")
                except Exception:
                    pass
        except Exception:      # 출처 테이블이 아직 없는 DB
            pass
        period = official_period(records)
    if not period:
        return False
    old = meta.get("broadcast_period") if isinstance(meta.get("broadcast_period"), dict) else {}
    if (old.get("start"), old.get("end") or "", old.get("raw")) == (period["start"], period["end"], period["raw"]) \
            and (cur_air or "") == period["start"]:
        return False
    meta["broadcast_period"] = period
    await db.execute("UPDATE dramas SET air_date=?, metadata_json=? WHERE id=?",
                     (period["start"], json.dumps(meta, ensure_ascii=False), drama_id))
    return True


async def merge_same_wiki_rows(db, drama_id: int) -> list[int]:
    """같은 위키 문서를 가진 '출처 없는 행'(위키 검색으로만 저장된 행)을 TVING/tvN 출처가 있는 행으로 합친다.

    예: 자동 수집이 'TVING 제목이 달라 위키를 못 찾은 행'(라이브(Live))을 만든 뒤, 같은 작품을 위키로 직접 검색해 저장한
    행(라이브)이 따로 생겼다가 위키가 붙으면서 두 행이 같은 문서를 가리키게 된 경우.
    - 시즌이 다르면(시즌2 ↔ 시즌3) 같은 문서여도 다른 작품이라 합치지 않는다.
    - 직접 수정한 항목이 있는 행, 양쪽 모두 출처가 있는 행은 합치지 않는다(자동 판단하지 않음).
    - 합친 뒤 남은 행(keeper)에 출처·인물·회차를 옮기고, 없어진 행의 id 목록을 돌려준다.
    """
    from core.database import canon_wiki_url, manual_fields_of
    from services.work_matcher import split_season, season_compatible
    cur = await db.execute("SELECT id, title, wiki_url, metadata_json FROM dramas WHERE id=?", (drama_id,))
    me = await cur.fetchone()
    if not me or not me[2]:
        return []
    url = canon_wiki_url(me[2])
    cur = await db.execute("SELECT id, title, wiki_url, metadata_json FROM dramas WHERE wiki_url IS NOT NULL AND wiki_url!='' ORDER BY id")
    group = [r for r in await cur.fetchall()
             if canon_wiki_url(r[2]) == url and season_compatible(split_season(r[1])[1], split_season(me[1])[1])]
    if len(group) < 2:
        return []

    async def n_sources(i):
        c = await db.execute("SELECT COUNT(*) FROM drama_sources WHERE drama_id=? AND source IN ('tving','tvn')", (i,))
        return (await c.fetchone())[0]

    def edited(r):
        try:
            return bool(manual_fields_of(json.loads(r[3] or "{}") or {}))
        except Exception:
            return False

    counts = {r[0]: await n_sources(r[0]) for r in group}
    keeper = max(group, key=lambda r: (counts[r[0]], -r[0]))
    removed = []
    for r in group:
        if r[0] == keeper[0] or counts[r[0]] > 0 or edited(r):
            continue
        did, kid = r[0], keeper[0]
        c = await db.execute("SELECT id, source, source_key FROM drama_sources WHERE drama_id=?", (did,))
        for sid, src, key in await c.fetchall():
            c2 = await db.execute("SELECT 1 FROM drama_sources WHERE drama_id=? AND source=? AND source_key=?", (kid, src, key))
            if await c2.fetchone():
                await db.execute("DELETE FROM drama_sources WHERE id=?", (sid,))
            else:
                await db.execute("UPDATE drama_sources SET drama_id=? WHERE id=?", (kid, sid))
        for tbl in ("drama_people", "drama_episodes"):
            c = await db.execute(f"SELECT DISTINCT source FROM {tbl} WHERE drama_id=?", (did,))
            for (src,) in await c.fetchall():
                c2 = await db.execute(f"SELECT 1 FROM {tbl} WHERE drama_id=? AND source=? LIMIT 1", (kid, src))
                if not await c2.fetchone():
                    await db.execute(f"UPDATE {tbl} SET drama_id=? WHERE drama_id=? AND source=?", (kid, did, src))
            await db.execute(f"DELETE FROM {tbl} WHERE drama_id=?", (did,))
        await db.execute("DELETE FROM drama_sources WHERE drama_id=?", (did,))
        await db.execute("DELETE FROM dramas WHERE id=?", (did,))
        removed.append(did)
    return removed
