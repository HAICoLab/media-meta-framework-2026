"""tvN ↔ TVING 교차 수집: 한쪽을 수집하면 다른 쪽에 같은 작품이 있는지 찾아서 한 작품으로 같이 만든다.

  tvN 로 시작  → tvN 페이지의 'TVING 다시보기' 링크(P코드)로 TVING 을 확정한다. 링크가 없으면 TVING 검색으로 후보를 찾고
                 제목·채널·연도 규칙(work_matcher)을 통과한 단 하나만 연결한다.
  TVING 로 시작 → tvN 프로그램 목록(캐시)에서 제목이 같은 후보를 찾아 각 후보 페이지의 TVING 링크를 확인한다.
                 코드가 같으면 확정(1.0), 링크가 없고 후보가 하나뿐이면 규칙 매칭(0.8), 코드가 다르면 거부한다.

수집은 BWC(broadcast_web_crawler) 의 tving.collect_program / tvn.collect_program 을 그대로 쓰고, 번들은
data/bwc/{tving,tvn}/manual_pair*/ 에 저장해 일괄 동기화(bwc_sync / tvn_sync) 때도 같은 파일이 재사용된다.

CLI:
    python -m services.cjenm_pair --tvn  <tvN URL>     [--apply]   # tvN 으로 시작
    python -m services.cjenm_pair --tving <P코드|URL>   [--apply]   # TVING 으로 시작
    python -m services.cjenm_pair --title <제목>        [--apply]   # 제목 검색(양쪽 모두 조회)
    python -m services.cjenm_pair --backfill [--limit N] [--apply]  # 이미 저장된 TVING 행에 tvN 연결
    python -m services.cjenm_pair --com2usn  [--limit N] [--apply]  # 이미 저장된 com2usN 행에 TVING/tvN 연결
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from services.tvn_bundle import extract_tving_code, parse_tvn_bundle
from services.tving_bundle import parse_tving_bundle
from services.work_matcher import channel_compatible, match_work, season_compatible, split_season, title_key

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_DATA = _ROOT / "data"
BWC_SRC = Path(os.environ.get("BWC_SRC", "/workspace/crawling_ykcho/broadcast_web_crawler/src"))
TVN_DIR = _DATA / "bwc" / "tvn" / "manual_pair"
TVING_DIR_FMT = _DATA / "bwc" / "tving" / "manual_pair_{kind}"
INVENTORY_CACHE = _DATA / "cache" / "tvn_inventory.json"
INVENTORY_TTL_SEC = 24 * 3600

_CONTENT_TYPE = {"예능": "variety", "드라마": "drama", "교양": "culture"}
_TVING_SEARCH = "https://gw.tving.com/bff/search/v2/content/total/search"
_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.tving.com/", "Origin": "https://www.tving.com",
       "Accept": "application/json"}
_MAX_VERIFY = 4          # 제목이 같은 후보가 많아도 페이지를 열어 확인하는 최대 개수


class PairError(RuntimeError):
    """수집·연결 중 사용자에게 보여줄 오류."""


class BlockedError(PairError):
    """수집 대상이 아닌 작품(Netflix 등)."""


# ── BWC 모듈 ──────────────────────────────────────────────────────────────────

def _bwc():
    if str(BWC_SRC) not in sys.path:
        sys.path.insert(0, str(BWC_SRC))
    try:
        from broadcast_web_crawler import tving, tvn
    except Exception as e:      # pragma: no cover - 환경 문제
        raise PairError(f"broadcast_web_crawler 를 불러올 수 없습니다: {e}") from e
    return tving, tvn


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w가-힣\-]+", "_", s or "").strip("_")[:60] or "untitled"


# ── tvN 프로그램 목록(캐시) ───────────────────────────────────────────────────

_inv_lock: asyncio.Lock | None = None


def _load_inventory_cache(max_age: float | None = INVENTORY_TTL_SEC) -> list[dict] | None:
    try:
        d = json.loads(INVENTORY_CACHE.read_text(encoding="utf-8"))
        if max_age is None or time.time() - d["fetched_at"] <= max_age:
            return d["programs"]
    except Exception:
        pass
    return None


def _fetch_inventory_sync() -> list[dict]:
    _, tvn = _bwc()
    report = tvn.discover_programs(tvn.LISTING_URL)
    if not report.get("complete_listing"):
        raise PairError("tvN 프로그램 목록을 끝까지 읽지 못했습니다.")
    progs = [{"program_id": p["program_id"], "title": p["title"], "url": p["url"],
              "listing_item": p["listing_item"]} for p in report["programs"]]
    INVENTORY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    INVENTORY_CACHE.write_text(json.dumps({"fetched_at": time.time(), "programs": progs}, ensure_ascii=False),
                               encoding="utf-8")
    return progs


async def tvn_inventory(refresh: bool = False) -> list[dict]:
    """tvN 전체 프로그램 목록. 24시간 캐시(전체 조회는 30초 이상 걸린다). 네트워크 실패 시 오래된 캐시라도 쓴다."""
    global _inv_lock
    if _inv_lock is None:
        _inv_lock = asyncio.Lock()
    async with _inv_lock:
        if not refresh:
            cached = _load_inventory_cache()
            if cached is not None:
                return cached
        try:
            return await asyncio.to_thread(_fetch_inventory_sync)
        except Exception as e:
            stale = _load_inventory_cache(max_age=None)
            if stale is not None:
                logger.warning(f"[pair] tvN 목록 갱신 실패, 오래된 캐시 사용: {e}")
                return stale
            raise PairError(f"tvN 프로그램 목록을 가져오지 못했습니다: {e}") from e


def _tvn_url_key(url: str) -> str:
    p = urlsplit(url or "")
    return (p.netloc + p.path).lower().rstrip("/")


async def tvn_entry_by_url(url: str) -> dict | None:
    key = _tvn_url_key(url)
    for refresh in (False, True):       # 캐시에 없으면(신규 작품) 한 번 갱신해서 다시 본다
        for p in await tvn_inventory(refresh=refresh):
            if _tvn_url_key(p["url"]) == key:
                return p
    return None


# ── 수집 (BWC 호출) ───────────────────────────────────────────────────────────

def _fetch_tving_sync(code: str) -> dict:
    tving, _ = _bwc()
    tmp = _DATA / "cache" / "pair_tmp" / code
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        bundle = tving.collect_program(f"https://www.tving.com/contents/{code}", tmp)
    except Exception as e:
        raise PairError(f"TVING 수집 실패({code}): {e}") from e
    rec = parse_tving_bundle(bundle)
    if not rec:
        raise PairError(f"TVING 페이지에서 작품 정보를 읽지 못했습니다({code})")
    dest = Path(str(TVING_DIR_FMT).format(kind=rec["kind"] or "기타")) / rec["source_key"]
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), str(dest))
    return bundle


def _fetch_tvn_sync(entry: dict) -> dict:
    _, tvn = _bwc()
    dest = TVN_DIR / _safe_name(entry["title"]) / "metadata.json"
    try:
        bundle = tvn.collect_program(entry["url"], dest, listing_item=entry["listing_item"])
    except Exception as e:
        raise PairError(f"tvN 수집 실패({entry['url']}): {e}") from e
    bundle["collection"]["channel_tab"] = "작품상세"
    from broadcast_web_crawler.tving_storage import write_bundle
    write_bundle(dest, bundle)
    return bundle


def _peek_tvn_tving_code_sync(url: str) -> str | None:
    """tvN 상세 페이지 한 장만 읽어 TVING 다시보기 코드를 꺼낸다 (미리보기 목록은 읽지 않음). 링크가 없으면 ''."""
    _, tvn = _bwc()
    props = tvn.parse_page(tvn._get_text(url))
    for m in props["fallback"].values():
        d = (m or {}).get("data") if isinstance(m, dict) else None
        if isinstance(d, dict) and isinstance(d.get("replayIngo"), dict):
            return extract_tving_code(d["replayIngo"].get("pgmReplayUrlAddr") or "")
    return ""


def _search_tving_sync(keyword: str) -> list[dict]:
    q = urlencode({"keyword": keyword, "pageNo": 1, "pageSize": 20, "screenCode": "CSSD0100",
                   "networkCode": "CSND0900", "osCode": "CSOD0900", "teleCode": "CSCD0900"})
    try:
        req = urllib.request.Request(f"{_TVING_SEARCH}?{q}", headers=_UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise PairError(f"TVING 검색 실패: {e}") from e
    out, seen = [], set()
    for band in (payload.get("data") or {}).get("bands") or []:
        for it in band.get("items") or []:
            code = str(it.get("code") or "")
            if code.startswith("P") and code not in seen:       # 프로그램(시리즈)만. 에피소드(E)·영화는 제외
                seen.add(code)
                out.append({"code": code, "title": it.get("title") or ""})
    return out


async def search_tving(keyword: str) -> list[dict]:
    return await asyncio.to_thread(_search_tving_sync, keyword)


async def fetch_tving(code: str) -> tuple[dict, dict]:
    """P/E 코드로 TVING 수집 → (번들, 정규화 레코드)."""
    bundle = await asyncio.to_thread(_fetch_tving_sync, code)
    return bundle, parse_tving_bundle(bundle)


async def fetch_tvn(entry: dict) -> tuple[dict, dict]:
    bundle = await asyncio.to_thread(_fetch_tvn_sync, entry)
    rec = parse_tvn_bundle(bundle)
    if not rec["source_key"]:
        raise PairError("tvN 작품 ID(pgmId)를 확인할 수 없습니다.")
    return bundle, rec


# ── 상대편 찾기 ───────────────────────────────────────────────────────────────

def _title_candidates(title: str, items: list[dict], key="title") -> list[dict]:
    """제목(시즌 제외)이 같고 시즌이 모순되지 않는 항목."""
    base, season = split_season(title)
    k = title_key(base)
    out = []
    for it in items:
        b, s = split_season(it.get(key) or "")
        if title_key(b) != k:
            continue
        if not season_compatible(season, s):
            continue
        out.append(it)
    return out


@dataclass
class Link:
    method: str                 # tving_code | tvn_link | title+channel+year | none | rejected | ambiguous
    confidence: float = 0.0
    note: str = ""


async def find_tving_for_tvn(rec: dict) -> tuple[dict | None, dict | None, Link]:
    """tvN 레코드 → (TVING 번들, TVING 레코드, 연결 정보)."""
    code = rec.get("tving_code") or ""
    if code:
        try:
            bundle, trec = await fetch_tving(code)
            return bundle, trec, Link("tving_code", 1.0, f"tvN 페이지의 TVING 링크 {code}")
        except PairError as e:
            note = f"TVING 링크({code}) 수집 실패 → 제목 검색으로 대체: {e}"
            logger.warning(f"[pair] {note}")
    else:
        note = ""
    base, _ = split_season(rec["title"])
    seen: set[str] = set()
    cands: list[dict] = []
    for kw in dict.fromkeys([rec["title"], base]):
        try:
            for it in await search_tving(kw):
                if it["code"] not in seen:
                    seen.add(it["code"]); cands.append(it)
        except PairError as e:
            return None, None, Link("none", 0.0, f"TVING 검색 실패: {e}")
    cands = _title_candidates(rec["title"], cands)
    years = sorted({int(e["posted_at"][:4]) for e in rec.get("episodes") or [] if e.get("posted_at")})
    ok: list[tuple[dict, dict]] = []
    for it in cands[:_MAX_VERIFY]:
        try:
            bundle, trec = await fetch_tving(it["code"])
        except PairError:
            continue
        if not channel_compatible("tvN", trec["channel"]):
            continue
        if trec["year"] and years and trec["year"] > max(years):
            continue
        ok.append((bundle, trec))
    if len(ok) == 1:
        return ok[0][0], ok[0][1], Link("title+channel+year", 0.8, "TVING 검색 결과 중 제목·채널·연도가 맞는 1건")
    if len(ok) > 1:
        return None, None, Link("ambiguous", 0.0, f"TVING 후보 {len(ok)}건 — 자동 연결 안 함")
    return None, None, Link("none", 0.0, note or "TVING 에서 같은 작품을 찾지 못함")


def _tvn_eligible(tving_rec: dict) -> bool:
    """tvN 공식 사이트에 있을 수 있는 작품인가 (채널이 tvN 계열이거나 TVING 전용/미상)."""
    return channel_compatible("tvN", tving_rec.get("channel") or "")


async def find_tvn_for_tving(trec: dict) -> tuple[dict | None, dict | None, Link]:
    """TVING 레코드 → (tvN 번들, tvN 레코드, 연결 정보)."""
    if not _tvn_eligible(trec):
        return None, None, Link("none", 0.0, f"채널 '{trec.get('channel')}' 은(는) tvN 대상이 아님")
    try:
        inv = await tvn_inventory()
    except PairError as e:
        return None, None, Link("none", 0.0, str(e))
    cands = _title_candidates(trec["title"], inv)
    if not cands:
        return None, None, Link("none", 0.0, "tvN 목록에 같은 제목이 없음")
    code = trec["source_key"]
    same, unlinked, other = [], [], []
    for entry in cands[:_MAX_VERIFY]:
        try:
            peek = await asyncio.to_thread(_peek_tvn_tving_code_sync, entry["url"])
        except Exception as e:
            logger.warning(f"[pair] tvN 페이지 확인 실패 {entry['url']}: {e}")
            continue
        (same if peek == code else unlinked if not peek else other).append(entry)
    pick, link = None, None
    if same:
        pick, link = same[0], Link("tving_code", 1.0, f"tvN 페이지의 TVING 링크가 {code} 와 일치")
    elif len(unlinked) == 1:
        pick, link = unlinked[0], Link("title+channel+year", 0.8, "tvN 목록에서 제목이 같은 1건 (TVING 링크 없음)")
    elif len(unlinked) > 1:
        return None, None, Link("ambiguous", 0.0, f"tvN 후보 {len(unlinked)}건 — 자동 연결 안 함")
    else:
        return None, None, Link("rejected", 0.0, "제목이 같은 tvN 작품은 다른 TVING 작품과 연결되어 있음")
    bundle, rec = await fetch_tvn(pick)
    return bundle, rec, link


# ── 저장 ──────────────────────────────────────────────────────────────────────

def _to_drama_from_tving(tving_bundle: dict, trec: dict) -> dict:
    from services.bwc_sync import bwc_metadata_to_drama
    drama = bwc_metadata_to_drama(tving_bundle, f"manual_pair_{trec['kind'] or '기타'}")
    if trec["kind"]:
        drama["content_type"] = _CONTENT_TYPE.get(trec["kind"], drama.get("content_type", ""))
    return drama


def _to_drama_from_tvn(rec: dict) -> dict:
    from services.tvn_sync import _to_drama
    return _to_drama(rec)


async def _wiki_enrich(drama: dict, hint: dict | None = None) -> None:
    from wiki.resolver import enrich_with_wikipedia
    from core.database import fill_meta_from_wiki
    title = drama.get("wiki_title") or drama["title"]
    ctype = drama.get("content_type") or ""
    if ctype in ("variety", "culture") and not (hint or {}).get("foreign") and not (hint or {}).get("origin"):
        hint = {**(hint or {}), "origin": "domestic"}   # tvN/티빙 예능·교양은 국내 제작물
    try:
        w = await enrich_with_wikipedia(title, content_type=ctype, hint=hint)
    except Exception as e:
        logger.warning(f"[pair] 위키 보충 실패 {title}: {e}")
        return
    if w and not w.get("no_wiki") and w.get("wiki_url"):
        drama["wikipedia"] = {k: w.get(k, d) for k, d in (
            ("wiki_url", ""), ("wiki_poster", ""), ("summary", ""), ("infobox", {}),
            ("sections", []), ("external_links", []))}
        fill_meta_from_wiki(drama["metadata"], w, title)
        if w.get("wiki_poster") and not drama["metadata"].get("poster_url"):
            drama["metadata"]["poster_url"] = w["wiki_poster"]
    elif w and w.get("no_wiki"):
        drama["wikipedia"] = {"no_wiki": True}
        drama["metadata"]["wiki_status"] = "none"


def _merge_new_drama(drama: dict, trec: dict | None, nrec: dict | None) -> dict:
    """새 작품 1건에 두 출처의 값을 합친다. 설명은 긴 쪽, 포스터는 TVING 우선(없으면 tvN), 출연은 TVING 우선."""
    meta = drama["metadata"]
    if trec and nrec:
        intro = nrec["intro_long"] or nrec["intent_short"]
        if len(intro) > len(meta.get("description") or ""):
            meta["description"] = intro
        if not meta.get("poster_url") and nrec["poster"]:
            meta["poster_url"] = nrec["poster"]
        if not meta.get("cast"):
            meta["cast"] = [p["name"] for p in nrec["people"] if p["kind"] == "cast"]
        meta.setdefault("extra", {})["source"] = "tving+tvn"
    return drama


async def _find_existing(db, trec: dict | None, nrec: dict | None) -> tuple[int | None, str, str]:
    """이미 저장된 같은 작품 찾기 → (drama_id | None, 방법, 사유). 방법이 ambiguous/rejected 이면 쓰지 않는다."""
    from core.source_tables import find_drama_id_by_source
    from services.tvn_sync import _db_candidates
    if trec:
        i = await find_drama_id_by_source(db, "tving", trec["source_key"])
        if i:
            return i, "source", "TVING 코드로 이미 저장됨"
    if nrec:
        i = await find_drama_id_by_source(db, "tvn", nrec["source_key"])
        if i:
            return i, "source", "tvN 작품 ID로 이미 저장됨"
    cands = await _db_candidates()
    probe = ({"title": trec["title"], "tving_code": trec["source_key"], "channel": trec["channel"],
              "years": [trec["year"]] if trec["year"] else []} if trec else
             {"title": nrec["title"], "tving_code": nrec.get("tving_code") or "", "channel": "tvN",
              "years": sorted({int(e["posted_at"][:4]) for e in nrec.get("episodes") or [] if e.get("posted_at")})})
    m = match_work(probe, cands)
    if m.method in ("tving_code", "title+channel+year") and (m.candidate or {}).get("id"):
        return m.candidate["id"], m.method, m.reason
    if m.method in ("ambiguous", "rejected"):
        return None, m.method, m.reason
    return None, "none", ""


async def _reconcile_official_url(db, drama_id: int) -> None:
    """공식 사이트 = tvN 공식 홈페이지가 있으면 tvN, 없으면 TVING. (직접 수정한 값은 건드리지 않는다)"""
    from core.database import manual_fields_of
    cur = await db.execute("SELECT official_site_url, metadata_json FROM dramas WHERE id=?", (drama_id,))
    row = await cur.fetchone()
    if not row:
        return
    try:
        if "official_site_url" in manual_fields_of(json.loads(row[1] or "{}") or {}):
            return
    except Exception:
        pass
    cur = await db.execute("SELECT url FROM drama_sources WHERE drama_id=? AND source='tvn' LIMIT 1", (drama_id,))
    tv = await cur.fetchone()
    if tv and tv[0] and tv[0] != row[0]:
        await db.execute("UPDATE dramas SET official_site_url=? WHERE id=?", (tv[0], drama_id))


async def _fill_blanks(db, drama_id: int, trec: dict | None, nrec: dict | None) -> None:
    """기존 행의 빈 칸만 새 출처 값으로 채운다 (직접 수정한 항목·이미 있는 값은 그대로)."""
    from core.database import manual_fields_of
    cur = await db.execute(
        "SELECT description, poster, director, writer, cast_json, genre, metadata_json FROM dramas WHERE id=?", (drama_id,))
    row = await cur.fetchone()
    if not row:
        return
    desc, poster, director, writer, cast_json, genre, meta_raw = row
    try:
        meta = json.loads(meta_raw or "{}") or {}
    except Exception:
        meta = {}
    manual = manual_fields_of(meta)
    sets: dict[str, object] = {}
    if nrec:
        intro = nrec["intro_long"] or nrec["intent_short"]
        if "description" not in manual and len(intro) > len(desc or ""):
            sets["description"] = intro
        if "poster" not in manual and not poster and nrec["poster"]:
            sets["poster"] = nrec["poster"]
        if "genre" not in manual and not genre and nrec["sub_genre"]:
            sets["genre"] = nrec["sub_genre"]
    if trec:
        if "poster" not in manual and not (sets.get("poster") or poster) and trec["poster"]:
            sets["poster"] = trec["poster"]
        if "director" not in manual and not director and trec["directors"]:
            sets["director"] = ", ".join(trec["directors"])
        if "writer" not in manual and not writer and trec["writers"]:
            sets["writer"] = ", ".join(trec["writers"])
        if "description" not in manual and not (sets.get("description") or desc) and trec["synopsis"]:
            sets["description"] = trec["synopsis"]
    cast_now = []
    try:
        cast_now = json.loads(cast_json or "[]") or []
    except Exception:
        pass
    if "cast" not in manual and not cast_now:
        names = [p["name"] for p in ((trec or {}).get("people") or []) if p["kind"] == "cast"] or \
                [p["name"] for p in ((nrec or {}).get("people") or []) if p["kind"] == "cast"]
        if names:
            sets["cast_json"] = json.dumps(names, ensure_ascii=False)
    if sets:
        await db.execute(f"UPDATE dramas SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?",
                         list(sets.values()) + [drama_id])


@dataclass
class PairOutcome:
    status: str                      # created | attached | conflict | error
    drama_id: int | None = None
    is_new: bool = False
    tving: dict | None = None        # 레코드
    tvn: dict | None = None
    tving_link: Link | None = None   # (tvN 으로 시작했을 때) TVING 연결 결과
    tvn_link: Link | None = None     # (TVING 으로 시작했을 때) tvN 연결 결과
    people: int = 0
    episodes: int = 0
    actors_linked: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        def brief(r):
            return {"key": r["source_key"], "title": r["title"], "url": r["url"]} if r else None
        return {"status": self.status, "drama_id": self.drama_id, "is_new": self.is_new,
                "tving": brief(self.tving), "tvn": brief(self.tvn),
                "tving_link": self.tving_link.__dict__ if self.tving_link else None,
                "tvn_link": self.tvn_link.__dict__ if self.tvn_link else None,
                "people": self.people, "episodes": self.episodes, "actors_linked": self.actors_linked,
                "notes": self.notes}


async def persist_pair(*, tving_bundle: dict | None = None, trec: dict | None = None,
                       tvn_bundle: dict | None = None, nrec: dict | None = None,
                       tving_link: Link | None = None, tvn_link: Link | None = None,
                       wiki_enrich: bool = True, drama_id: int | None = None,
                       create: bool = True) -> PairOutcome:
    """찾은 두 출처를 한 작품으로 저장한다. 이미 있는 작품이면 새로 만들지 않고 붙인다.
    drama_id 를 주면(호출한 쪽이 이미 대상 행을 확정한 경우) 그 행에 붙인다. create=False 면 기존 행이 없을 때 새로 만들지 않는다."""
    import aiosqlite
    from core import database
    from core.database import upsert_drama
    from core.source_tables import ensure_tables, save_source_record
    out = PairOutcome("error", tving=trec, tvn=nrec, tving_link=tving_link, tvn_link=tvn_link)
    if not (trec or nrec):
        out.notes.append("저장할 출처가 없음")
        return out

    async with aiosqlite.connect(database.DB_PATH) as db:
        await ensure_tables(db)
        await db.commit()
        if drama_id is not None:
            how, why = "given", ""
        else:
            drama_id, how, why = await _find_existing(db, trec, nrec)

    if how in ("ambiguous", "rejected"):
        # 시즌/채널이 달라 '다른 작품'으로 판정됐더라도, 제목 문자열까지 완전히 같은 행이 있으면
        # upsert_drama(제목 기준 갱신)가 그 행을 덮어쓰므로 만들지 않는다. 제목이 다르면 별개 작품으로 새로 만든다.
        new_title = ((trec or nrec) or {}).get("title", "")
        async with aiosqlite.connect(database.DB_PATH) as db:
            same = await (await db.execute("SELECT 1 FROM dramas WHERE title=?", (new_title,))).fetchone()
        if how == "ambiguous" or same:
            out.status = "conflict"
            out.notes.append(f"같은 제목의 다른 작품이 있어 자동 병합하지 않음: {why}")
            return out
        out.notes.append(f"비슷한 제목의 다른 작품과 구분해 새로 만듦: {why}")
        drama_id = None

    if drama_id is None and not create:
        out.status = "skipped"
        out.notes.append("기존 작품이 없어 새로 만들지 않음")
        return out

    if drama_id is None:
        drama = _to_drama_from_tving(tving_bundle, trec) if trec else _to_drama_from_tvn(nrec)
        drama = _merge_new_drama(drama, trec, nrec)
        if wiki_enrich:
            await _wiki_enrich(drama, (drama["metadata"].get("extra") or {}).get("wiki_hint"))
        drama_id, _ = await upsert_drama(drama)
        out.is_new, out.status = True, "created"
    else:
        out.status = "attached"

    async with aiosqlite.connect(database.DB_PATH) as db:
        if trec:
            m = tving_link or Link("origin", 1.0)
            n = await save_source_record(db, drama_id, trec, method=m.method, confidence=m.confidence or 1.0)
            out.people += n["people"]; out.episodes += n["episodes"]
        if nrec:
            m = tvn_link or Link("origin", 1.0)
            n = await save_source_record(db, drama_id, nrec, method=m.method, confidence=m.confidence or 1.0)
            out.people += n["people"]; out.episodes += n["episodes"]
        if not out.is_new:
            await _fill_blanks(db, drama_id, trec, nrec)
        await _reconcile_official_url(db, drama_id)
        from core.source_tables import apply_official_period
        await apply_official_period(db, drama_id)      # 방영일은 위키 → TVING → tvN 순
        from core.database import sync_full_meta
        await sync_full_meta(db, drama_id)             # metadata 에 TVING·tvN 레코드를 통째로 반영
        await db.commit()
        try:
            from wiki.actors import link_actors
            out.actors_linked = await link_actors(db, drama_id)
            await db.commit()
        except Exception as e:
            logger.warning(f"[pair] 배우 연결 실패(id={drama_id}): {e}")
    out.drama_id = drama_id
    return out


# ── 진입점 ────────────────────────────────────────────────────────────────────

def _is_tvn_url(url: str) -> bool:
    return urlsplit((url or "").strip()).netloc.lower() == "tvn.cjenm.com"


_TVING_ANY_CODE_RE = re.compile(r"(?<![0-9A-Za-z])([PE]\d{6,})(?![0-9A-Za-z])")


def _tving_host(url: str) -> bool:
    t = (url or "").strip()
    if t and "://" not in t:
        t = "https://" + t                      # 'tving.com/contents/P…' 처럼 scheme 없이 붙여넣은 경우
    h = urlsplit(t).netloc.lower().split(":")[0]
    return h == "tving.com" or h.endswith(".tving.com") or h == "tving.page.link"


def _tving_code_from_url(url: str) -> str:
    """TVING 링크에서 작품(P)·회차(E) 코드를 뽑는다. /contents/ 뿐 아니라 /program/, /player/, /onboarding/, 앱 공유 링크도 받는다."""
    code = extract_tving_code(url or "")
    if code:
        return code
    from urllib.parse import unquote
    m = _TVING_ANY_CODE_RE.search(unquote(url or ""))
    return m.group(1) if m else ""


async def resolve_tving_page(url: str) -> str:
    """작품 코드가 URL 에 없는 TVING 페이지(예: /service/ip/days100 같은 작품 전용 홍보관)에서 대표 작품 코드(programCode)를 찾아
    'https://www.tving.com/contents/P…' 로 돌려준다. 못 찾으면 빈 문자열."""
    t = (url or "").strip()
    if t and "://" not in t:
        t = "https://" + t
    if not _tving_host(t):
        return ""

    def _get() -> str:
        req = urllib.request.Request(t, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "ignore")

    try:
        html = await asyncio.to_thread(_get)
    except Exception as e:
        logger.info(f"[pair] TVING 페이지 열기 실패: {e}")
        return ""
    m = re.search(r'programCode\\*"\s*:\s*\\*"(P\d+)', html)
    return f"https://www.tving.com/contents/{m.group(1)}" if m else ""


def _is_tving_url(url: str) -> bool:
    return _tving_host(url) and bool(_tving_code_from_url(url))


def is_pair_url(url: str) -> bool:
    return _is_tvn_url(url) or _is_tving_url(url)


async def collect_from_tvn(url: str, *, wiki_enrich: bool = True, entry: dict | None = None) -> PairOutcome:
    _, tvn = _bwc()
    try:
        norm = tvn.normalize_url(url)
        if tvn.is_listing(norm):
            raise PairError("tvN 작품 상세 URL 을 입력해 주세요 (프로그램 목록 URL 은 지원하지 않음).")
    except ValueError as e:
        raise PairError(str(e)) from e
    entry = entry or await tvn_entry_by_url(norm)
    if not entry:
        raise PairError("tvN 프로그램 목록에서 이 작품을 찾지 못했습니다. URL 을 확인해 주세요.")
    nbundle, nrec = await fetch_tvn(entry)
    tbundle, trec, link = await find_tving_for_tvn(nrec)
    out = await persist_pair(tving_bundle=tbundle, trec=trec, tvn_bundle=nbundle, nrec=nrec,
                             tving_link=link, tvn_link=Link("origin", 1.0), wiki_enrich=wiki_enrich)
    out.tving_link = link
    if not trec:
        out.notes.append(f"TVING: {link.note}")
    return out


async def collect_from_tving(code_or_url: str, *, wiki_enrich: bool = True) -> PairOutcome:
    text = code_or_url.strip()
    code = text if re.fullmatch(r"[PE]\d+", text) else _tving_code_from_url(text)
    if not code:
        raise PairError("TVING 작품 코드(P…)를 확인할 수 없습니다.")
    tbundle, trec = await fetch_tving(code)
    if _channel_blocked(trec["channel"]):
        raise BlockedError("Netflix 작품은 수집하지 않습니다.")
    nbundle, nrec, link = await find_tvn_for_tving(trec)
    out = await persist_pair(tving_bundle=tbundle, trec=trec, tvn_bundle=nbundle, nrec=nrec,
                             tving_link=Link("origin", 1.0), tvn_link=link, wiki_enrich=wiki_enrich)
    out.tvn_link = link
    if not nrec:
        out.notes.append(f"tvN: {link.note}")
    return out


async def collect_from_url(url: str, *, wiki_enrich: bool = True) -> PairOutcome:
    if _is_tvn_url(url):
        return await collect_from_tvn(url, wiki_enrich=wiki_enrich)
    if _is_tving_url(url):
        return await collect_from_tving(url, wiki_enrich=wiki_enrich)
    raise PairError("tvN 또는 TVING 작품 URL 이 아닙니다.")


def _channel_blocked(ch: str) -> bool:
    return "netflix" in (ch or "").lower()      # Netflix 는 어떤 경로로도 수집하지 않는다


async def find_by_title(title: str) -> tuple[str, dict] | None:
    """제목으로 tvN 목록과 TVING 검색을 본다. 제목(시즌 포함)이 같은 후보가 정확히 하나일 때만 ('tvn', 항목) / ('tving', 항목)."""
    title = (title or "").strip()
    if not title:
        return None
    try:
        inv = await tvn_inventory()
    except PairError:
        inv = []
    hits = _title_candidates(title, inv)
    if len(hits) > 1:        # 시즌별로 여러 개면 제목이 완전히 같은 것만
        hits = [h for h in hits if title_key(h["title"]) == title_key(title)]
    if len(hits) == 1:
        return "tvn", hits[0]
    if hits:
        return None
    try:
        results = await search_tving(title)
    except PairError:
        return None
    thits = [h for h in _title_candidates(title, results) if title_key(h["title"]) == title_key(title)]
    return ("tving", thits[0]) if len(thits) == 1 else None


async def collect_by_title(title: str, *, wiki_enrich: bool = True, origin: str = "") -> PairOutcome | None:
    """제목 검색 → 어느 쪽에서 찾았든 상대편까지 찾아 같이 저장. 못 찾으면 None (호출한 쪽이 위키 검색으로 이어간다).
    origin("domestic"/"foreign")을 주면 반대편으로 확인되는 작품은 저장하지 않고 None 을 돌려준다(tvN 은 국내, TVING 은 자체 분류로 판단)."""
    found = await find_by_title(title)
    if not found:
        return None
    side, item = found
    if origin in ("domestic", "foreign"):
        if side == "tvn":
            if origin == "foreign":
                return None
        else:
            try:
                _, _rec = await fetch_tving(item["code"])
            except Exception:
                _rec = None
            if _rec:
                from services.origin import detect_origin
                _g = detect_origin({"tving": _rec})["group"]
                if _g in ("domestic", "foreign") and _g != origin:
                    return None
    if side == "tvn":
        return await collect_from_tvn(item["url"], wiki_enrich=wiki_enrich, entry=item)
    try:
        return await collect_from_tving(item["code"], wiki_enrich=wiki_enrich)
    except BlockedError:
        return None


async def attach_tvn_to_saved_tving(drama_id: int, trec: dict, tving_bundle: dict | None = None) -> PairOutcome | None:
    """bwc_sync 용: 이미 저장된 TVING 작품(drama_id)에 tvN 공식 홈페이지 정보를 찾아 붙인다."""
    nbundle, nrec, link = await find_tvn_for_tving(trec)
    if not nrec:
        return None
    return await persist_pair(tving_bundle=tving_bundle, trec=trec, tvn_bundle=nbundle, nrec=nrec,
                              tving_link=Link("origin", 1.0), tvn_link=link, wiki_enrich=False, drama_id=drama_id)


_TOPUP_TRIED: dict[int, float] = {}
TOPUP_RETRY_SEC = 3600       # 상대편을 못 찾은 작품은 이 시간 동안 다시 네트워크로 찾지 않는다


async def complete_saved(drama_id: int, *, force: bool = False) -> PairOutcome | None:
    """이미 저장된 작품에 한쪽 출처만 있으면(TVING 만 / tvN 만) 상대편을 찾아 내용을 보충한다.
    둘 다 있거나, 상대편을 못 찾았으면 None. 못 찾은 작품은 TOPUP_RETRY_SEC 동안 다시 찾지 않는다."""
    import aiosqlite
    from core import database
    from core.source_tables import ensure_tables
    now = time.time()
    if not force and now - _TOPUP_TRIED.get(drama_id, 0) < TOPUP_RETRY_SEC:
        return None
    async with aiosqlite.connect(database.DB_PATH) as db:
        await ensure_tables(db)
        cur = await db.execute("SELECT source, record_json FROM drama_sources WHERE drama_id=?", (drama_id,))
        recs = {src: rj for src, rj in await cur.fetchall()}
    has_tving, has_tvn = "tving" in recs, "tvn" in recs
    if not has_tving and not has_tvn:     # 둘 다 없음: com2usN 작품이면 제목·채널·연도로 찾고, 그 외는 제목 검색 쪽에서 처리
        _TOPUP_TRIED[drama_id] = now
        try:
            res = await link_com2usn(drama_id, apply=True)
        except Exception as e:
            logger.warning(f"[pair] com2usN 연결 실패(id={drama_id}): {e}")
            return None
        return PairOutcome("attached", drama_id, tving=res.tving, tvn=res.tvn, notes=res.notes) if res.status == "linked" else None
    if has_tving == has_tvn:          # 둘 다 있음(보충 불필요)
        return None
    _TOPUP_TRIED[drama_id] = now
    try:
        if has_tving:
            out = await attach_tvn_to_saved_tving(drama_id, json.loads(recs["tving"]))
        else:
            nrec = json.loads(recs["tvn"])
            tbundle, trec, link = await find_tving_for_tvn(nrec)
            out = None
            if trec and not _channel_blocked(trec.get("channel") or ""):
                out = await persist_pair(tving_bundle=tbundle, trec=trec, nrec=nrec, tving_link=link,
                                         tvn_link=Link("origin", 1.0), wiki_enrich=False, drama_id=drama_id)
    except PairError as e:
        return None
    return out


async def backfill(limit: int = 0, apply: bool = False) -> list[dict]:
    """저장된 TVING 번들(로컬 data/bwc/tving) 전체에 대해 tvN 상대편을 찾아 연결한다. apply=False 면 찾기만."""
    root = _DATA / "bwc" / "tving"
    results: list[dict] = []
    seen: set[str] = set()
    paths = sorted(root.rglob("metadata.json")) if root.exists() else []
    for p in paths:
        try:
            bundle = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        trec = parse_tving_bundle(bundle)
        if not trec or trec["source_key"] in seen or _channel_blocked(trec["channel"]):
            continue
        seen.add(trec["source_key"])
        if not _tvn_eligible(trec):          # tvN 대상이 아닌 작품(MBC 등)은 TVING 출처만 기존 행에 남긴다
            row = {"tving": trec["source_key"], "title": trec["title"], "channel": trec["channel"],
                   "tvn": None, "tvn_title": None, "link": "none", "note": f"채널 '{trec['channel']}' 은(는) tvN 대상이 아님"}
            if apply:
                out = await persist_pair(tving_bundle=bundle, trec=trec, tving_link=Link("origin", 1.0), wiki_enrich=False, create=False)
                row.update(status=out.status, drama_id=out.drama_id)
            results.append(row)
            continue
        if limit and len(results) >= limit:
            break
        nbundle, nrec, link = await find_tvn_for_tving(trec)
        row = {"tving": trec["source_key"], "title": trec["title"], "channel": trec["channel"],
               "tvn": nrec["source_key"] if nrec else None, "tvn_title": nrec["title"] if nrec else None,
               "link": link.method, "note": link.note}
        if apply:
            out = await persist_pair(tving_bundle=bundle, trec=trec, tvn_bundle=nbundle, nrec=nrec,
                                     tving_link=Link("origin", 1.0), tvn_link=link if nrec else None,
                                     wiki_enrich=True, create=bool(nrec))
            row.update(status=out.status, drama_id=out.drama_id, is_new=out.is_new, notes=out.notes)
        results.append(row)
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────


# ── com2usN 작품 ↔ TVING / tvN ────────────────────────────────────────────────
# com2usN 은 공식 사이트에 제목·YouTube·포스터뿐이라, 같은 작품이 TVING / tvN 에 있으면 거기서 줄거리·출연·회차를 보충한다.
# 저장된 com2usN 행(dramas.broadcaster='com2usN')을 그대로 두고(제목·포스터 유지) TVING·tvN 출처를 그 행에 붙인다.

_COM2USN_GENERIC = {"", "tving", "com2usn", "컴투스엔"}


def _row_year(air_date: str) -> int | None:
    m = re.search(r"(\d{4})", air_date or "")
    return int(m.group(1)) if m else None


def _com2usn_channel_ok(row_channel: str, other_channel: str) -> bool:
    """com2usN 행(위키에서 얻은 채널)과 TVING 채널이 같은 작품일 수 있는가. 한쪽이 미상·TVING 이면 통과, 아니면 같은 계열이어야 한다."""
    a, b = (row_channel or "").strip().lower(), (other_channel or "").strip().lower()
    if a in _COM2USN_GENERIC or b in _COM2USN_GENERIC or a == b:
        return True
    return channel_compatible(a, b)


@dataclass
class Com2usnLink:
    drama_id: int
    title: str
    status: str                       # linked | skipped | none | conflict | error
    tving: dict | None = None
    tvn: dict | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        brief = lambda r: {"key": r["source_key"], "title": r["title"], "channel": r.get("channel"), "url": r["url"]} if r else None
        return {"drama_id": self.drama_id, "title": self.title, "status": self.status,
                "tving": brief(self.tving), "tvn": brief(self.tvn), "notes": self.notes}


async def find_pair_for_com2usn(title: str, channel: str, air_date: str):
    """com2usN 작품(제목·채널·방영일) → (TVING 번들, TVING 레코드, tvN 번들, tvN 레코드, 메모[]).
    제목(시즌 포함)이 같고 채널이 양립하며 방영 연도가 ±1 이내인 후보가 정확히 하나일 때만 연결한다.
    방영 연도를 모르면 오연결 위험이 커서 연결하지 않는다."""
    notes: list[str] = []
    year = _row_year(air_date)
    if _channel_blocked(channel) or "넷플릭스" in (channel or ""):
        return None, None, None, None, ["Netflix 작품은 연결하지 않음"]
    if not year:
        return None, None, None, None, ["방영 연도를 몰라 연결하지 않음"]

    # TVING
    tbundle = trec = None
    base, _ = split_season(title)
    seen: set[str] = set()
    cands: list[dict] = []
    for kw in dict.fromkeys([title, base]):
        try:
            for it in await search_tving(kw):
                if it["code"] not in seen:
                    seen.add(it["code"]); cands.append(it)
        except PairError as e:
            notes.append(f"TVING 검색 실패: {e}")
            break
    ok: list[tuple[dict, dict]] = []
    for it in _title_candidates(title, cands)[:_MAX_VERIFY]:
        try:
            b, r = await fetch_tving(it["code"])
        except PairError:
            continue
        if _channel_blocked(r["channel"]):
            continue
        if not _com2usn_channel_ok(channel, r["channel"]):
            notes.append(f"TVING {it['code']} 채널 불일치({r['channel']}≠{channel})")
            continue
        if r["year"] and abs(r["year"] - year) > 1:
            notes.append(f"TVING {it['code']} 연도 불일치({r['year']}≠{year})")
            continue
        ok.append((b, r))
    if len(ok) == 1:
        tbundle, trec = ok[0]
    elif len(ok) > 1:
        notes.append(f"TVING 후보 {len(ok)}건 — 자동 연결 안 함")
    elif not notes:
        notes.append("TVING 에서 같은 작품을 찾지 못함")

    # tvN
    nbundle = nrec = None
    if trec:
        if _tvn_eligible(trec):
            nbundle, nrec, link = await find_tvn_for_tving(trec)
            if not nrec:
                notes.append(f"tvN: {link.note}")
    elif channel_compatible("tvN", channel if (channel or "").strip().lower() not in _COM2USN_GENERIC else ""):
        probe = {"title": title, "channel": channel, "source_key": ""}
        nbundle, nrec, link = await find_tvn_for_tving(probe)
        if nrec:
            years = sorted({int(e["posted_at"][:4]) for e in nrec.get("episodes") or [] if e.get("posted_at")})
            if years and year > max(years) + 1:
                notes.append(f"tvN 연도 불일치({years}≠{year})")
                nbundle = nrec = None
        else:
            notes.append(f"tvN: {link.note}")
    return tbundle, trec, nbundle, nrec, notes


async def link_com2usn(drama_id: int, *, apply: bool = False) -> Com2usnLink:
    """저장된 com2usN 작품에 같은 TVING / tvN 작품을 찾아 붙인다. apply=False 면 찾기만 한다."""
    import aiosqlite
    from core import database
    from core.source_tables import ensure_tables, find_drama_id_by_source
    async with aiosqlite.connect(database.DB_PATH) as db:
        await ensure_tables(db)
        cur = await db.execute("SELECT title, channel, air_date, broadcaster FROM dramas WHERE id=?", (drama_id,))
        row = await cur.fetchone()
        cur = await db.execute("SELECT source FROM drama_sources WHERE drama_id=?", (drama_id,))
        have = {r[0] for r in await cur.fetchall()}
    if not row:
        return Com2usnLink(drama_id, "", "error", notes=["작품 없음"])
    title, channel, air_date, broadcaster = row
    out = Com2usnLink(drama_id, title, "none")
    if (broadcaster or "") != "com2usN":
        out.status = "skipped"; out.notes.append("com2usN 작품이 아님"); return out
    if {"tving", "tvn"} <= have:
        out.status = "skipped"; out.notes.append("이미 TVING·tvN 연결됨"); return out

    tbundle, trec, nbundle, nrec, notes = await find_pair_for_com2usn(title, channel, air_date)
    out.notes += notes
    if not (trec or nrec):
        return out
    out.tving, out.tvn = trec, nrec
    # 같은 TVING/tvN 작품이 이미 다른 행에 저장돼 있으면 출처를 빼앗지 않고 알린다(중복 행 정리 대상)
    async with aiosqlite.connect(database.DB_PATH) as db:
        for rec in (trec, nrec):
            if rec:
                other = await find_drama_id_by_source(db, rec["source"], rec["source_key"])
                if other and other != drama_id:
                    out.status = "conflict"
                    out.notes.append(f"{rec['source']} {rec['source_key']} 은(는) 이미 다른 작품(id={other})에 저장됨 — 중복 행 확인 필요")
                    return out
    out.status = "linked"
    if not apply:
        return out
    res = await persist_pair(tving_bundle=tbundle, trec=trec, tvn_bundle=nbundle, nrec=nrec,
                             tving_link=Link("title+channel+year", 0.8, "com2usN 제목·채널·연도"),
                             tvn_link=Link("title+channel+year", 0.8, "com2usN 제목·채널·연도"),
                             wiki_enrich=False, drama_id=drama_id)
    if res.status not in ("attached", "created"):
        out.status = "error"; out.notes.append(f"저장 실패: {res.status} {res.notes}")
    return out


async def backfill_com2usn(limit: int = 0, apply: bool = False) -> list[dict]:
    """저장된 com2usN 작품 전체에 대해 TVING / tvN 연결을 시도한다."""
    import aiosqlite
    from core import database
    async with aiosqlite.connect(database.DB_PATH) as db:
        cur = await db.execute("SELECT id FROM dramas WHERE broadcaster='com2usN' ORDER BY id")
        ids = [r[0] for r in await cur.fetchall()]
    results = []
    for i in ids[: limit or None]:
        try:
            results.append((await link_com2usn(i, apply=apply)).summary())
        except Exception as e:
            results.append({"drama_id": i, "status": "error", "notes": [str(e)]})
    return results


async def _dry(from_side: str, key: str) -> dict:
    """저장하지 않고 연결 결과만 본다."""
    if from_side == "tvn":
        _, tvn = _bwc()
        entry = await tvn_entry_by_url(tvn.normalize_url(key))
        if not entry:
            return {"error": "tvN 목록에 없음"}
        _, nrec = await fetch_tvn(entry)
        _, trec, link = await find_tving_for_tvn(nrec)
        return {"tvn": [nrec["source_key"], nrec["title"], nrec["tving_code"]],
                "tving": [trec["source_key"], trec["title"], trec["channel"]] if trec else None, "link": link.__dict__}
    code = key if re.fullmatch(r"[PE]\d+", key) else extract_tving_code(key)
    _, trec = await fetch_tving(code)
    _, nrec, link = await find_tvn_for_tving(trec)
    return {"tving": [trec["source_key"], trec["title"], trec["channel"]],
            "tvn": [nrec["source_key"], nrec["title"], nrec["url"]] if nrec else None, "link": link.__dict__}


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--tvn"); g.add_argument("--tving"); g.add_argument("--title"); g.add_argument("--backfill", action="store_true")
    g.add_argument("--com2usn", action="store_true", help="저장된 com2usN 작품에 TVING/tvN 연결")
    ap.add_argument("--apply", action="store_true", help="DB 에 저장 (없으면 연결 결과만 출력)")
    ap.add_argument("--no-wiki", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    async def run():
        if a.com2usn:
            for r in await backfill_com2usn(limit=a.limit, apply=a.apply):
                print(json.dumps(r, ensure_ascii=False))
            return
        if a.backfill:
            rows = await backfill(limit=a.limit, apply=a.apply)
            for r in rows:
                print(json.dumps(r, ensure_ascii=False))
            return
        if not a.apply and a.title:
            print(json.dumps(await find_by_title(a.title), ensure_ascii=False, default=str, indent=2)[:1500])
            return
        if not a.apply:
            print(json.dumps(await _dry("tvn" if a.tvn else "tving", a.tvn or a.tving), ensure_ascii=False, indent=2))
            return
        if a.tvn:
            out = await collect_from_tvn(a.tvn, wiki_enrich=not a.no_wiki)
        elif a.tving:
            out = await collect_from_tving(a.tving, wiki_enrich=not a.no_wiki)
        else:
            out = await collect_by_title(a.title, wiki_enrich=not a.no_wiki)
            if out is None:
                print("제목이 같은 단일 작품을 tvN/TVING 에서 찾지 못함"); return
        print(json.dumps(out.summary(), ensure_ascii=False, indent=2))

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
