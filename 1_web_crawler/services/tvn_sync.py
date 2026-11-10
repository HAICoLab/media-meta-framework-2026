"""tvN 공식 홈페이지 번들 → 라이브러리(dramas + 출처 테이블) 동기화.

흐름: 번들 스캔 → parse_tvn_bundle → 후보 목록(기존 dramas 행 + 아직 반영 안 된 TVING 번들)과 매칭
      → 확정/자동 매칭이면 같은 작품에 tvN 원본·인물·회차를 붙이고, 매칭 후보가 없으면 새 작품으로 만든다.
      모호/거부(같은 제목의 다른 채널 등)는 어떤 행도 만들거나 덮어쓰지 않고 '확인 필요'로 보고만 한다.

사용:
    python -m services.tvn_sync              # dry-run (DB 변경 없음, 매칭 보고서만 출력)
    python -m services.tvn_sync --apply      # 반영 (--no-wiki 로 위키 보충 생략)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

from services.tvn_bundle import parse_tvn_bundle, extract_tving_code
from services.work_matcher import match_tvn_record, MatchResult

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
TVN_ROOT = _ROOT / "data" / "bwc" / "tvn"
TVING_ROOT = _ROOT / "data" / "bwc" / "tving"

_CONTENT_TYPE = {"예능": "variety", "드라마": "drama", "교양": "culture"}


def scan_tvn_bundles() -> list[Path]:
    return sorted(TVN_ROOT.rglob("metadata.json")) if TVN_ROOT.exists() else []


def _year_of(text: str) -> int | None:
    m = re.search(r"(19|20)\d{2}", text or "")
    return int(m.group(0)) if m else None


async def _db_candidates() -> list[dict]:
    import aiosqlite
    from core import database
    out = []
    async with aiosqlite.connect(database.DB_PATH) as db:
        cur = await db.execute("SELECT id, title, channel, air_date, official_site_url FROM dramas")
        rows = await cur.fetchall()
        # tvN 링크로 이미 알려진 TVING 코드(drama_sources, source='tving')도 후보 키로 쓴다
        known: dict[int, str] = {}
        try:
            cur = await db.execute("SELECT drama_id, source_key FROM drama_sources WHERE source='tving'")
            known = {d: k for d, k in await cur.fetchall()}
        except Exception:
            pass
        for _id, title, channel, air_date, url in rows:
            out.append({"id": _id, "title": title, "channel": channel or "", "year": _year_of(air_date or ""),
                        "tving_code": extract_tving_code(url or "") or known.get(_id, ""), "origin": "db"})
    return out


def _tving_bundle_candidates(exclude_codes: set[str], db_cands: list[dict] | None = None) -> list[dict]:
    """아직 DB 에 반영되지 않은 TVING 번들도 후보에 넣어 '이 작품은 TVING 반영 후 합쳐진다'를 미리 알린다.
    같은 작품이 여러 수집 폴더에 있으면 최신 하나만, DB 에 같은 제목·채널 행이 이미 있으면(반영 시 그 행에 합쳐지므로) 넣지 않는다."""
    from services.work_matcher import channel_compatible, title_key
    db_keys = [(title_key(c["title"]), c["channel"]) for c in (db_cands or [])]
    by_code: dict[str, dict] = {}
    for p in sorted(TVING_ROOT.rglob("metadata.json")) if TVING_ROOT.exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        ci = d.get("contentInfo") or {}
        url = (d.get("collection") or {}).get("source_url") or ""
        code = extract_tving_code(url)
        if not code or code in exclude_codes:
            continue
        title, channel = ci.get("title") or "", ci.get("channel") or ""
        if any(k == title_key(title) and channel_compatible(c, channel) for k, c in db_keys):
            continue
        by_code[code] = {"id": None, "title": title, "channel": channel,
                         "year": _year_of(str(ci.get("broad_dt") or ci.get("product_year") or "")),
                         "tving_code": code, "origin": "tving_bundle", "path": str(p)}   # 정렬 순서상 뒤(최신)가 이긴다
    return list(by_code.values())


async def plan(include_tving_bundles: bool = True) -> list[tuple[dict, MatchResult]]:
    cands = await _db_candidates()
    if include_tving_bundles:
        cands += _tving_bundle_candidates({c["tving_code"] for c in cands if c["tving_code"]}, cands)
    known_tvn = await _known_tvn_keys()
    latest: dict[str, dict] = {}
    for p in scan_tvn_bundles():
        try:
            rec = parse_tvn_bundle(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"[tvn_sync] 번들 파싱 실패 {p}: {e}")
            continue
        if not rec["title"] or not rec["source_key"]:
            continue
        prev = latest.get(rec["source_key"])
        if not prev or (rec["collected_at"] or "") >= (prev["collected_at"] or ""):
            latest[rec["source_key"]] = rec      # 같은 작품을 여러 번 수집했으면 가장 최근 것만
    out = []
    for rec in latest.values():
        if rec["source_key"] in known_tvn:      # 이미 이 tvN 작품이 저장·연결된 행이 있으면 다시 매칭하지 않는다 (제목이 바뀌었어도 안전)
            out.append((rec, MatchResult("tvn_key", 1.0, {"id": known_tvn[rec["source_key"]]}, "이미 저장된 tvN 작품")))
        else:
            out.append((rec, match_tvn_record(rec, cands)))
    return out


async def _known_tvn_keys() -> dict[str, int]:
    import aiosqlite
    from core import database
    async with aiosqlite.connect(database.DB_PATH) as db:
        try:
            cur = await db.execute("SELECT source_key, drama_id FROM drama_sources WHERE source='tvn'")
            return {k: d for k, d in await cur.fetchall()}
        except Exception:
            return {}


def _to_drama(rec: dict) -> dict:
    cast = [p["name"] for p in rec["people"] if p["kind"] == "cast"]
    return {
        "title": rec["title"],
        "broadcaster": "CJ ENM",
        "content_type": _CONTENT_TYPE.get(rec["kind"], ""),
        "official_site_url": rec["url"],
        "wikipedia": {},
        "metadata": {
            "poster_url": rec["poster"],
            "description": rec["intro_long"] or rec["intent_short"],
            "channel": "tvN",
            "cast": cast,
            "director": [], "writer": [],
            "genre": rec["sub_genre"],
            "broadcast_period": {}, "episode_count": None,
            "extra": {"source": "tvn", "tvn_status": rec["status"], "air_time_text": rec["air_time_text"]},
        },
    }


async def apply(plans: list[tuple[dict, MatchResult]], wiki_enrich: bool = True, link_tving: bool = True) -> dict:
    """매칭 결과를 DB 에 반영한다. 저장은 cjenm_pair.persist_pair 가 맡는다(tvN·TVING 을 한 작품으로).

    - 기존 작품과 매칭(확정/자동)  → 그 작품에 tvN 원본·인물·회차를 붙인다.
    - 로컬 TVING 번들만 있고 DB 에 없음 → TVING 번들로 작품을 만들고 tvN 을 붙인다.
    - 매칭 없음 → (link_tving 이면) TVING 에서 같은 작품을 찾아 같이 만들고, 못 찾으면 tvN 만으로 만든다.
    - 모호/거부(같은 제목의 다른 채널 등) → 아무것도 하지 않고 '확인 필요'로 센다.
    """
    from services import cjenm_pair as pair
    from services.tving_bundle import parse_tving_bundle

    stats = {"attached": 0, "created": 0, "review": 0, "tving_linked": 0, "people": 0, "episodes": 0, "actors": 0}
    for rec, m in plans:
        tving_bundle = trec = None
        tving_link = None
        drama_id = None
        if m.method in ("tving_code", "title+channel+year", "tvn_key"):
            cand = m.candidate or {}
            drama_id = cand.get("id")
            if drama_id is None and cand.get("path"):        # 로컬 TVING 번들 → 작품을 TVING 쪽으로 만들고 tvN 을 붙인다
                tving_bundle = json.loads(Path(cand["path"]).read_text(encoding="utf-8"))
                trec = parse_tving_bundle(tving_bundle)
                tving_link = pair.Link(m.method, m.confidence, m.reason)
            elif drama_id is not None and link_tving:     # 기존 행에 붙일 때, TVING 코드를 알면 TVING 출처(인물·회차)도 같이 남긴다
                code = cand.get("tving_code") or rec.get("tving_code") or ""
                if code:
                    try:
                        tving_bundle, trec = await pair.fetch_tving(code)
                        tving_link = pair.Link("tving_code" if rec.get("tving_code") == code else "origin", 1.0)
                    except Exception as e:
                        logger.warning(f"[tvn_sync] TVING 출처 수집 실패 {code}: {e}")
                        tving_bundle = trec = None
        elif m.method in ("none", "rejected"):    # rejected: 시즌·채널이 달라 다른 작품으로 판정 → 새 작품(persist_pair 가 제목 충돌만 재확인)
            if link_tving:
                try:
                    tving_bundle, trec, tving_link = await pair.find_tving_for_tvn(rec)
                except Exception as e:
                    logger.warning(f"[tvn_sync] TVING 연결 실패 {rec['title']}: {e}")
        else:                         # ambiguous → 건드리지 않는다
            stats["review"] += 1
            continue

        out = await pair.persist_pair(
            tving_bundle=tving_bundle, trec=trec, nrec=rec, tving_link=tving_link,
            tvn_link=pair.Link(m.method if m.method not in ("none", "rejected", "tvn_key") else "origin", m.confidence or 1.0),
            wiki_enrich=wiki_enrich, drama_id=drama_id)
        if out.status == "conflict":
            stats["review"] += 1
            continue
        stats["created" if out.is_new else "attached"] += 1
        stats["tving_linked"] += 1 if trec else 0
        stats["people"] += out.people
        stats["episodes"] += out.episodes
        stats["actors"] += out.actors_linked
    return stats


def _print_report(plans):
    for rec, m in plans:
        cand = m.candidate or {}
        target = (f"→ {cand.get('origin')} id={cand.get('id')} '{cand.get('title')}' [{cand.get('channel')}]"
                  if cand else (f"({m.reason})" if m.reason else ""))
        print(f"{rec['title'][:20]:22}{rec['kind']:3} {rec['status']:5} tving={rec['tving_code'] or '-':11} "
              f"{m.method:20}{m.confidence:>4} {target}")
    from collections import Counter
    print("\n요약:", dict(Counter(m.method for _, m in plans)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-wiki", action="store_true")
    ap.add_argument("--no-tving-link", action="store_true", help="매칭 없는 tvN 작품의 TVING 상대편을 찾지 않는다")
    args = ap.parse_args()
    plans = asyncio.run(plan())
    _print_report(plans)
    if args.apply:
        print("\n반영:", asyncio.run(apply(plans, wiki_enrich=not args.no_wiki, link_tving=not args.no_tving_link)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
