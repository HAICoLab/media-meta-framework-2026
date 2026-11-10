"""
BWC (broadcast_web_crawler) → dramas 테이블 동기화 서비스

TVING/tvN/나무위키에서 수집된 metadata.json 파일을 읽어
dramas 테이블에 저장하고 위키피디아로 후처리합니다.

Wikipedia 후처리 흐름:
  contentInfo.title → ko.wikipedia.org 검색 → infobox/요약/포스터 보충
"""

import json
import logging
import re
from typing import Optional

from core.paths import DATA_DIR

logger = logging.getLogger(__name__)

_BWC_ROOT = DATA_DIR / "bwc"

# TVING 이미지 CDN 기본 URL
_TVING_CDN = "https://image.tving.com"


# ── 이미지 URL 정규화 ──────────────────────────────────────────────────────────

def _normalize_image(url: str) -> str:
    """상대 URL이면 TVING CDN 기반으로 절대 URL 변환."""
    if not url:
        return ""
    if url.startswith("http"):
        # /dims/... 파라미터 제거해 원본 크기 이미지 반환
        url = re.sub(r"/dims/.*$", "", url)
        return url
    # 상대 경로면 CDN 붙임
    return _TVING_CDN + url


def _best_image(images: list) -> str:
    """contentInfo.image 리스트에서 가장 적합한 포스터 URL 선택.
    CAIP0400(포스터) > CAIP0200(썸네일) > 첫 번째 이미지 순서로 우선.
    """
    preferred = {}
    first = ""
    for img in (images or []):
        code = img.get("code", "")
        raw_url = img.get("url", "") or img.get("url2", "")
        url = _normalize_image(raw_url)
        if not url:
            continue
        if not first:
            first = url
        preferred[code] = url

    return (
        preferred.get("CAIP0400")   # 세로 포스터
        or preferred.get("CAIP0600")
        or preferred.get("CAIP0200")
        or first
    )


# ── metadata.json → drama dict 변환 ──────────────────────────────────────────

def _build_wiki_hint(ci: dict, jld: dict) -> dict:
    """TVING 메타데이터 → 위키 문서 선택 힌트.

    - foreign/country: category_name '해외드라마', genre_name '일본드라마' 등에서 추출
    - year: 방영 연도 (jsonLd dateCreated/startDate → broad_dt → product_year)
    - channel: 티빙 채널(방송사). 해외작품은 재배급처라 resolver 가 비교에서 제외한다
    같은 제목의 리메이크·타국 작품이 있을 때(예: 일본 '닥터X' vs 한국판) 문서를 가려내는 단서다.
    """
    category = str(ci.get("category_name") or "")
    genre_name = str(ci.get("genre_name") or "")
    foreign = "해외" in category
    country = ""
    if foreign:
        m = re.match(r"^(.+?)\s*드라마$", genre_name)
        if m and m.group(1) not in ("해외",):
            country = m.group(1)

    year = None
    for raw in (jld.get("dateCreated"), jld.get("startDate"), ci.get("broad_dt"), ci.get("product_year")):
        m = re.match(r"^\s*(\d{4})", str(raw or ""))
        if m:
            year = int(m.group(1))
            break
    hint = {"foreign": foreign, "country": country, "year": year,
            "channel": str(ci.get("channel") or "").strip()}
    # 티빙에는 해외 예능·교양 카테고리가 없다 → 예능/교양은 국내 제작물. 같은 단어로 시작하는
    # 해외 프로그램 문서(예: 언더커버셰프 → 영국 '언더커버 보스')로 잘못 연결되는 것을 막는다.
    if not foreign and category in ("예능", "교양"):
        hint["origin"] = "domestic"
    return hint


def bwc_metadata_to_drama(meta: dict, collection_name: str = "") -> Optional[dict]:
    """BWC metadata.json 하나를 dramas upsert 형식으로 변환.

    Returns:
        드라마 데이터 dict, 또는 제목 없으면 None
    """
    ci = meta.get("contentInfo", {})
    jld_list = meta.get("jsonLd", [])
    jld = jld_list[0] if isinstance(jld_list, list) and jld_list else (
          jld_list if isinstance(jld_list, dict) else {})

    col_info = meta.get("collection", {})

    title = ci.get("title") or jld.get("name") or ""
    if not title:
        return None

    # 시즌 표기를 뺀 제목은 위키 검색에만 쓴다 (예: "유미의 세포들 시즌3" → "유미의 세포들").
    # 라이브러리 제목은 시즌을 그대로 둬야 시즌별로 따로 저장된다(안 그러면 시즌2·3 이 한 행에 덮어써진다).
    clean_title = re.sub(r"\s*(시즌\d+|Season\s*\d+|S\d+)$", "", title, flags=re.IGNORECASE).strip()
    title = title.strip()

    # 채널 / 방송사
    channel = ci.get("channel") or ""

    # 출연진
    cast = ci.get("actor") or []

    # 연출/극본
    director = ", ".join(ci.get("director") or [])
    writer = ", ".join(ci.get("script") or []) if ci.get("script") else ""

    # 장르
    genre = jld.get("genre") or ci.get("keywords") or ""

    # 시놉시스
    synopsis = ci.get("synopsis") or jld.get("description") or ""

    # 포스터 이미지
    poster = _best_image(ci.get("image") or [])
    if not poster:
        # jsonLd image 폴백
        jld_img = jld.get("image") or ""
        poster = _normalize_image(jld_img) if jld_img else ""

    # 공식 사이트 URL (TVING 작품 페이지)
    official_url = col_info.get("source_url") or jld.get("url") or ""
    # TVING URL이 /contents/E... (에피소드)이면 /contents/P... (프로그램)으로 치환
    official_url = re.sub(r"/contents/E\w+", lambda m: m.group().replace("/E", "/P"), official_url)

    # 방송사 판별
    broadcaster = _detect_broadcaster(channel, official_url, collection_name)

    wiki_hint = _build_wiki_hint(ci, jld)

    # content_type: jsonLd @type 기반
    content_type = "variety" if "예능" in collection_name else "drama"
    if "예능" in (genre or ""):
        content_type = "variety"

    return {
        "title":             title,
        "wiki_title":        clean_title or title,
        "broadcaster":       broadcaster,
        "content_type":      content_type,
        "official_site_url": official_url,
        "wikipedia":         {},  # Wikipedia 후처리에서 채움
        "metadata": {
            "poster_url":      poster,
            "description":     synopsis,
            "channel":         channel,
            "cast":            cast if isinstance(cast, list) else [cast],
            "director":        [d.strip() for d in director.split(",") if d.strip()],
            "writer":          [w.strip() for w in writer.split(",") if w.strip()],
            "genre":           genre,
            "broadcast_period": {},
            "episode_count":   None,
            # 출처별 고유 필드는 extra 에 모은다
            "extra": {
                "source":    "bwc",
                "wiki_hint": wiki_hint,   # 위키 문서 선택 시 같은 제목의 다른 작품을 거르는 데 쓴다
            },
        },
        "_raw_bwc": {
            "content_code": ci.get("code", ""),
            "collection":   collection_name,
        },
    }


def _detect_broadcaster(channel: str, url: str, collection: str) -> str:
    """채널/URL에서 방송사 판별."""
    ch = channel.lower()
    if "tvn" in ch or "tvn" in url.lower():
        return "CJ ENM"
    if "tving" in url.lower():
        return "CJ ENM"
    if "ocn" in ch:
        return "OCN"
    if "jtbc" in ch:
        return "JTBC"
    if "mbc" in ch:
        return "MBC"
    if "sbs" in ch:
        return "SBS"
    if "kbs" in ch:
        return "KBS"
    if "namuwiki" in collection or "나무위키" in collection:
        return ""
    return "CJ ENM"  # TVING 기본값




# ── 컬렉션 스캔 ──────────────────────────────────────────────────────────────

def scan_bwc_collections(collection_name: str = "") -> list[dict]:
    """BWC 데이터 디렉토리를 스캔해 metadata.json 파일 목록 반환.

    Args:
        collection_name: 특정 컬렉션만 스캔 (빈 문자열이면 전체)

    Returns:
        [{"path": Path, "collection_name": str}, ...]
    """
    results = []
    if not _BWC_ROOT.exists():
        return results

    for meta_file in _BWC_ROOT.rglob("metadata.json"):
        try:
            parts = meta_file.parts
            col = parts[-3] if len(parts) >= 3 else ""
            # 특정 컬렉션 필터
            if collection_name and col != collection_name:
                continue
            results.append({
                "path":            meta_file,
                "collection_name": col,
            })
        except Exception:
            pass
    return results


# ── Wikipedia 후처리 ──────────────────────────────────────────────────────────
# 위키 문서 선택/본문 수집은 수동검색·com2usN 과 공용인 wiki.resolver 를 사용합니다.
# (같은 제목이면 어느 경로에서든 같은 문서, 같은 본문)
from wiki.resolver import enrich_with_wikipedia  # noqa: E402,F401


async def _sync_tvn_bundle(raw: dict, wiki_enrich: bool, link_tving: bool = True) -> dict:
    """tvN 번들 하나를 라이브러리에 반영. TVING 에 같은 작품이 있으면 함께 저장한다."""
    from services import cjenm_pair
    from services.tvn_bundle import parse_tvn_bundle
    try:
        nrec = parse_tvn_bundle(raw)
        if not nrec.get("title") or not nrec.get("source_key"):
            return {"status": "skipped", "title": nrec.get("title", ""), "why": "제목/작품 ID 없음"}
        tbundle = trec = tlink = None
        if link_tving:
            try:
                tbundle, trec, tlink = await cjenm_pair.find_tving_for_tvn(nrec)
            except Exception as e:
                logger.warning(f"[bwc_sync] TVING 연결 실패 ({nrec['title']}): {e}")
        out = await cjenm_pair.persist_pair(
            tving_bundle=tbundle, trec=trec, tvn_bundle=raw, nrec=nrec, tving_link=tlink,
            tvn_link=cjenm_pair.Link("origin", 1.0), wiki_enrich=wiki_enrich)
        status = {"created": "created", "attached": "attached"}.get(out.status, "skipped")
        return {"status": status, "title": nrec["title"], "drama_id": out.drama_id, "tving": bool(trec), "notes": out.notes}
    except Exception as e:
        logger.warning(f"[bwc_sync] tvN 번들 반영 실패: {e}")
        return {"status": "error", "title": "", "why": str(e)}


def latest_collection(names) -> str:
    """수집 폴더 이름 중 가장 최근 것. 날짜(YYYYMMDD_…)로 시작하는 폴더만 대상으로 한다
    ('manual_pair' 같은 수동검색 보관 폴더가 문자열 비교로 '최신'이 되지 않게)."""
    dated = [n for n in names if re.match(r"^\d{8}_", n or "")]
    return max(dated) if dated else ""


async def _save_tving_source(drama_id: int, raw: dict, link_tvn: bool) -> None:
    """저장된 TVING 작품에 출처 레코드(인물·회차 포함)를 남기고, tvN 계열이면 tvN 상대편도 찾아 붙인다.
    실패해도 작품 저장 자체는 유지한다."""
    import aiosqlite
    from core import database
    from core.source_tables import ensure_tables, save_source_record
    from services.tving_bundle import parse_tving_bundle
    try:
        trec = parse_tving_bundle(raw)
        if not trec:
            return
        async with aiosqlite.connect(database.DB_PATH) as db:
            await ensure_tables(db)
            await save_source_record(db, drama_id, trec, method="origin", confidence=1.0)
            from core.source_tables import apply_official_period
            await apply_official_period(db, drama_id)      # 방영일은 위키 → TVING → tvN 순
            await db.commit()
            try:      # TVING 은 배역 정보가 없으므로 위키 '등장 인물'로 인물·배역을 채운다
                from wiki.actors import link_actors
                await link_actors(db, drama_id)
                await db.commit()
            except Exception as e:
                logger.warning(f"[bwc_sync] 배우 연결 실패(drama_id={drama_id}): {e}")
        if link_tvn:
            from services import cjenm_pair
            if cjenm_pair._tvn_eligible(trec):
                await cjenm_pair.attach_tvn_to_saved_tving(drama_id, trec, raw)
    except Exception as e:
        logger.warning(f"[bwc_sync] 출처 저장/tvN 연결 실패 (drama_id={drama_id}): {e}")


# ── 동기화 메인 함수 ──────────────────────────────────────────────────────────

async def sync_collection_to_library(
    collection_name: str,
    wiki_enrich: bool = True,
    on_progress=None,
    link_tvn: bool = True,
) -> dict:
    """특정 BWC 컬렉션만 dramas 테이블에 동기화 + Wikipedia 보충.

    BWC 크롤 완료 직후 자동으로 호출됩니다.
    중복 항목은 upsert로 업데이트됩니다.

    Args:
        collection_name: 동기화할 컬렉션 이름 (예: 20261029_155151_드라마_전체)
        wiki_enrich:     True면 Wikipedia 보충 수행
        on_progress:     콜백 (current, total, message)
        link_tvn:        True면 저장한 TVING 작품마다 tvN 공식 홈페이지의 같은 작품을 찾아 함께 저장
    """
    files = scan_bwc_collections(collection_name=collection_name)
    if not files:
        logger.warning(f"[bwc_sync] 컬렉션 '{collection_name}' metadata.json 없음")
        return {"saved": 0, "skipped": 0, "errors": 0, "total": 0}

    return await sync_bwc_to_library(
        wiki_enrich=wiki_enrich,
        on_progress=on_progress,
        files_override=files,
        link_tvn=link_tvn,
    )


async def sync_bwc_to_library(
    wiki_enrich: bool = True,
    on_progress=None,
    existing_titles: set | None = None,
    files_override: list | None = None,
    link_tvn: bool = True,
) -> dict:
    """BWC 수집 데이터를 dramas 테이블에 동기화.

    Args:
        wiki_enrich:     True이면 Wikipedia 후처리 수행
        on_progress:     콜백 (current, total, message)
        existing_titles: 이미 DB에 있는 제목 집합 (중복 방지, None이면 스킵 없이 모두 upsert)
        files_override:  None이면 전체 스캔, list이면 해당 파일만 처리
        link_tvn:        True면 TVING 작품마다 tvN 공식 홈페이지 상대편을 찾아 같이 저장(채널이 tvN 계열일 때만 시도)

    Returns:
        {"saved": int, "skipped": int, "errors": int, "total": int}
    """
    from core.database import upsert_drama, fill_meta_from_wiki

    files = files_override if files_override is not None else scan_bwc_collections()
    total = len(files)
    saved = skipped = errors = 0

    if on_progress:
        on_progress(0, total, f"BWC 파일 {total}개 스캔 완료")

    for i, info in enumerate(files):
        try:
            raw = json.loads(info["path"].read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"[bwc_sync] 파일 읽기 실패 {info['path']}: {e}")
            errors += 1
            continue

        # tvN 공식 홈페이지 번들 — TVING 번들과 형식이 달라 따로 처리한다 (TVING 상대편을 찾아 같이 저장)
        if raw.get("format") == "tvn-source-bundle":
            res = await _sync_tvn_bundle(raw, wiki_enrich, link_tving=link_tvn)
            if res["status"] == "created":
                saved += 1
            elif res["status"] == "error":
                errors += 1
            else:
                skipped += 1
            if on_progress:
                on_progress(i + 1, total, f"{res.get('title', '')} (tvN)")
            continue

        drama = bwc_metadata_to_drama(raw, info["collection_name"])
        if not drama:
            skipped += 1
            continue

        title = drama["title"]

        # 중복 확인 (이미 DB에 있으면 스킵)
        if existing_titles and title in existing_titles:
            skipped += 1
            if on_progress:
                on_progress(i + 1, total, f"{title} (기존)")
            continue

        # Wikipedia 후처리
        if wiki_enrich:
            _ctype = drama.get("content_type") or drama.get("metadata", {}).get("content_type") or ""
            wiki_data = await enrich_with_wikipedia(
                drama.get("wiki_title") or title, content_type=_ctype, hint=(drama.get("metadata", {}).get("extra") or {}).get("wiki_hint"))
            if wiki_data and wiki_data.get("no_wiki"):
                # 위키 문서가 없는 경우: no_wiki 플래그를 그대로 넘겨 upsert 가 '문서 없음'으로 저장
                # (summary 에 안내 문구가 들어 있어 그대로 옮기면 '위키 있음'으로 오인된다)
                drama["wikipedia"] = {"no_wiki": True}
                drama["metadata"]["wiki_status"] = "none"
            elif wiki_data:
                drama["wikipedia"] = {
                    "wiki_url":       wiki_data.get("wiki_url", ""),
                    "wiki_poster":    wiki_data.get("wiki_poster", ""),
                    "summary":        wiki_data.get("summary", ""),
                    "infobox":        wiki_data.get("infobox", {}),
                    "sections":       wiki_data.get("sections", []),   # 전체 본문 섹션
                    "external_links": wiki_data.get("external_links", []),
                }
                # 위키 인포박스로 메타데이터의 빈 항목을 보충 (장르·연출·극본·출연 + 방송 기간/시간/국가·제작사 등)
                meta = drama["metadata"]
                fill_meta_from_wiki(meta, wiki_data, drama.get("wiki_title") or title)
                # 위키 포스터가 있고 TVING 포스터가 없으면 사용
                if wiki_data.get("wiki_poster") and not meta.get("poster_url"):
                    meta["poster_url"] = wiki_data["wiki_poster"]

        try:
            drama_id, is_new = await upsert_drama(drama)
            await _save_tving_source(drama_id, raw, link_tvn)
            if is_new:
                saved += 1
                if existing_titles is not None:
                    existing_titles.add(title)
            else:
                skipped += 1
        except Exception as e:
            logger.warning(f"[bwc_sync] 저장 실패 ({title}): {e}")
            errors += 1

        if on_progress:
            on_progress(i + 1, total, f"{title}")

    logger.info(f"[bwc_sync] 완료: 저장={saved}, 스킵={skipped}, 오류={errors} / 총={total}")
    return {"saved": saved, "skipped": skipped, "errors": errors, "total": total}


# ── Wikipedia 보충: 기존 저장된 CJ ENM 드라마에 위키 보충 ─────────────────────

async def enrich_library_with_wikipedia(
    broadcaster: str = "CJ ENM",
    max_items: int = 0,            # 0=전체
    on_progress=None,
    delay_sec: float = 0.8,        # 요청 사이 딜레이
    only_ids: list[int] | None = None,   # 지정하면 이 id 의 행만(수집이 덜 된 행 재수집용, 방송사/조건 무시)
) -> dict:
    """dramas 테이블에서 wiki_url이 없는 CJ ENM 항목을 가져와 위키피디아로 보충.

    Args:
        broadcaster:  대상 방송사 필터
        only_ids:     특정 행만 다시 수집할 때의 id 목록
        max_items:    최대 처리 수 (0=전체)
        on_progress:  콜백 (current, total, message)
        delay_sec:    API 요청 간 딜레이 (초)

    Returns:
        {"enriched": int, "skipped": int, "errors": int, "total": int}
    """
    import asyncio
    import aiosqlite
    from core.database import DB_PATH

    # wiki_url이 없거나 wiki_sections_json이 없는 드라마 목록 조회 (content_type 포함)
    # (이미 wiki_url이 있어도 sections가 비어있으면 재보충)
    async with aiosqlite.connect(DB_PATH) as db:
        if only_ids:
            _ph = ",".join("?" for _ in only_ids)
            cur = await db.execute(
                f"SELECT id, title, content_type, metadata_json FROM dramas WHERE id IN ({_ph}) ORDER BY id DESC",
                list(only_ids),
            )
        elif broadcaster:
            cur = await db.execute(
                """SELECT id, title, content_type, metadata_json FROM dramas
                   WHERE broadcaster=?
                     AND (wiki_url IS NULL OR wiki_url=''
                          OR wiki_sections_json IS NULL OR wiki_sections_json='' OR wiki_sections_json='[]')
                   ORDER BY id DESC""",
                (broadcaster,),
            )
        else:
            cur = await db.execute(
                """SELECT id, title, content_type, metadata_json FROM dramas
                   WHERE wiki_url IS NULL OR wiki_url=''
                      OR wiki_sections_json IS NULL OR wiki_sections_json='' OR wiki_sections_json='[]'
                   ORDER BY id DESC"""
            )
        rows = await cur.fetchall()

    if max_items > 0:
        rows = rows[:max_items]

    total = len(rows)
    enriched = skipped = errors = 0

    if on_progress:
        on_progress(0, total, f"위키피디아 보충 대상: {total}개")

    for i, (drama_id, title, content_type_db, _meta_raw) in enumerate(rows):
        if on_progress:
            on_progress(i, total, f"위키 검색: {title}")

        try:  # 수집 때 저장해 둔 힌트(해외작품 여부·국가·방영연도)로 같은 제목의 다른 작품을 거른다
            _mm = json.loads(_meta_raw or "{}") or {}
            _hint = (_mm.get("extra") or {}).get("wiki_hint") or _mm.get("wiki_hint")   # 통일 이전 형식도 읽는다
        except Exception:
            _hint = None
        if content_type_db in ("variety", "culture") and not (_hint or {}).get("foreign") and not (_hint or {}).get("origin"):
            _hint = {**(_hint or {}), "origin": "domestic"}   # 예능·교양은 국내 제작물(티빙에 해외 예능 없음)
        wiki_data = await enrich_with_wikipedia(title, content_type=content_type_db or "", hint=_hint)

        if not wiki_data:
            skipped += 1
            await asyncio.sleep(delay_sec * 0.5)
            continue

        # 위키 문서가 없는 경우 → '문서 없음' 상태를 명시적으로 기록하고 다음으로
        if wiki_data.get("no_wiki"):
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    cur_d = await db.execute("SELECT description, metadata_json FROM dramas WHERE id=?", (drama_id,))
                    row_d = await cur_d.fetchone()
                    # 사용자가 직접 수정한 '위키 상태'가 있으면 자동 판정으로 바꾸지 않는다
                    from core.database import manual_fields_of as _mf_of
                    try:
                        _manual_d = _mf_of(json.loads((row_d[1] if row_d else None) or "{}") or {})
                    except Exception:
                        _manual_d = set()
                    if "wiki_status" in _manual_d or "description" in _manual_d:
                        skipped += 1
                        await asyncio.sleep(delay_sec * 0.5)
                        continue
                    try:
                        _m = json.loads((row_d[1] if row_d else None) or "{}") or {}
                    except Exception:
                        _m = {}
                    # 컬럼 description 이 비었거나 안내문뿐이면: 수집처 설명(TVING 소개글 등)이 있으면 그것을, 없으면 안내문을 둔다
                    # (안내문이 수집처 설명을 가리지 않게 한다)
                    if not (row_d and row_d[0] and row_d[0] != "위키피디아 문서가 없습니다"):
                        await db.execute(
                            "UPDATE dramas SET description=? WHERE id=?",
                            (_m.get("description") or "위키피디아 문서가 없습니다", drama_id)
                        )
                    # 설명이 있어도 카드에 '위키피디아 문서 없음'을 표시할 수 있도록
                    # 상태를 metadata_json.wiki_status 에 남긴다
                    _m["wiki_status"] = "none"
                    from core.database import normalize_drama_meta as _norm_meta
                    await db.execute(
                        "UPDATE dramas SET metadata_json=? WHERE id=?",
                        (json.dumps(_norm_meta(_m), ensure_ascii=False), drama_id),
                    )
                    await db.commit()
            except Exception as _e:
                logger.debug(f"[wiki_enrich] '{title}' no_wiki 업데이트 실패: {_e}")
            skipped += 1
            await asyncio.sleep(delay_sec * 0.5)
            continue

        if not (wiki_data.get("wiki_url") or wiki_data.get("summary")):
            skipped += 1
            await asyncio.sleep(delay_sec * 0.5)
            continue

        # DB 업데이트 (wiki_url, description, poster, infobox_json)
        try:
            import json as _json
            async with aiosqlite.connect(DB_PATH) as db:
                # wiki_url 업데이트
                from core.database import canon_wiki_url as _canon, fill_meta_from_wiki as _fill_meta
                updates = {"wiki_url": _canon(wiki_data.get("wiki_url")) or None}
                if updates["wiki_url"]:
                    # 같은 문서를 이미 다른 행이 쓰고 있으면(제목만 다른 같은 작품) 알려 둔다
                    _cur_dup = await db.execute("SELECT id, title, wiki_url FROM dramas WHERE id!=? AND wiki_url IS NOT NULL AND wiki_url!=''", (drama_id,))
                    for _did, _dt, _du in await _cur_dup.fetchall():
                        if _canon(_du) == updates["wiki_url"]:
                            logger.warning(f"[wiki_enrich] '{title}' 은(는) 이미 '{_dt}'(id={_did}) 가 쓰는 위키 문서와 같습니다")
                            break
                # external_links → official_site_url 폴백 (기존 없을 때만)
                _ext_links = wiki_data.get("external_links") or []
                _ext_url = None
                for _lnk in _ext_links:
                    _u = _lnk.get("url") if isinstance(_lnk, dict) else (_lnk if isinstance(_lnk, str) else "")
                    if _u and _u.startswith("http"):
                        _ext_url = _u
                        break
                if _ext_url:
                    cur_off = await db.execute("SELECT official_site_url FROM dramas WHERE id=?", (drama_id,))
                    row_off = await cur_off.fetchone()
                    if not (row_off and row_off[0]):
                        updates["official_site_url"] = _ext_url
                if wiki_data.get("summary"):
                    # 기존 description이 없을 때만 wiki summary로 대체
                    cur2 = await db.execute("SELECT description FROM dramas WHERE id=?", (drama_id,))
                    row2 = await cur2.fetchone()
                    # '문서 없음' 안내문은 설명이 아니므로 위키 요약으로 교체한다
                    if not (row2 and row2[0]) or row2[0] == "위키피디아 문서가 없습니다":
                        updates["description"] = wiki_data["summary"]
                if wiki_data.get("wiki_poster"):
                    # 기존 poster가 없을 때만 wiki poster 사용
                    cur3 = await db.execute("SELECT poster FROM dramas WHERE id=?", (drama_id,))
                    row3 = await cur3.fetchone()
                    if not (row3 and row3[0]):
                        updates["poster"] = wiki_data["wiki_poster"]
                if wiki_data.get("infobox"):
                    from core.database import clean_infobox as _clean_ib
                    updates["infobox_json"] = _json.dumps(_clean_ib(wiki_data["infobox"]), ensure_ascii=False)
                if wiki_data.get("director") and isinstance(wiki_data["director"], list):
                    dir_str = ", ".join(wiki_data["director"])
                    cur4 = await db.execute("SELECT director FROM dramas WHERE id=?", (drama_id,))
                    row4 = await cur4.fetchone()
                    if not (row4 and row4[0]):
                        updates["director"] = dir_str
                if wiki_data.get("cast") and isinstance(wiki_data["cast"], list):
                    cur5 = await db.execute("SELECT cast_json FROM dramas WHERE id=?", (drama_id,))
                    row5 = await cur5.fetchone()
                    if not (row5 and row5[0]):
                        updates["cast_json"] = _json.dumps(wiki_data["cast"], ensure_ascii=False)
                # 위키피디아 전체 섹션 저장 (항상 최신 값으로 덮어씀)
                if wiki_data.get("sections"):
                    updates["wiki_sections_json"] = _json.dumps(wiki_data["sections"], ensure_ascii=False)
                # status 업데이트
                updates["status"] = "ok"
                # 위키 상태 기록 (이전에 '문서 없음'이었다면 해제)
                cur6 = await db.execute("SELECT metadata_json FROM dramas WHERE id=?", (drama_id,))
                row6 = await cur6.fetchone()
                try:
                    _m2 = _json.loads((row6[0] if row6 else None) or "{}") or {}
                except Exception:
                    _m2 = {}
                # 사용자가 직접 수정한 항목은 위키 보충으로 덮어쓰지 않는다
                from core.database import manual_fields_of as _mf_of2, _EDIT_FIELD_MAP as _EFM
                _manual = _mf_of2(_m2)
                for _f in _manual:
                    for _c in _EFM[_f][0]:
                        updates.pop(_c, None)
                if "wiki_status" not in _manual:
                    _m2["wiki_status"] = "ok"
                # 위키 인포박스로 메타데이터의 빈 항목을 채운다 (방송 기간·시간·국가·제작사 등, 직접 수정한 항목은 제외)
                _fill_meta(_m2, wiki_data, title)
                from core.database import normalize_drama_meta as _norm_meta2
                updates["metadata_json"] = _json.dumps(_norm_meta2(_m2), ensure_ascii=False)

                sets = ", ".join(f"{k}=?" for k in updates)
                vals = list(updates.values()) + [drama_id]
                await db.execute(f"UPDATE dramas SET {sets} WHERE id=?", vals)
                from core.source_tables import apply_official_period
                await apply_official_period(db, drama_id)      # 방영일은 위키 → TVING → tvN 순
                from core.source_tables import merge_same_wiki_rows
                _merged = await merge_same_wiki_rows(db, drama_id)   # 위키가 붙어 같은 문서를 가진 '출처 없는 행'이 생겼으면 합친다
                if _merged:
                    logger.info(f"[wiki_enrich] '{title}' 와 같은 위키 문서를 쓰는 중복 행 {_merged} 을(를) 합쳤습니다")
                await db.commit()
            enriched += 1
            logger.info(f"[wiki_enrich] [{i+1}/{total}] '{title}' → {wiki_data.get('wiki_url','')}")
        except Exception as e:
            logger.warning(f"[wiki_enrich] '{title}' DB 저장 실패: {e}")
            errors += 1

        # Rate limit 방지 딜레이
        await asyncio.sleep(delay_sec)

    if on_progress:
        on_progress(total, total, f"완료 ({enriched}개 보충)")

    logger.info(f"[wiki_enrich] 완료: 보충={enriched}, 스킵={skipped}, 오류={errors} / 총={total}")
    return {"enriched": enriched, "skipped": skipped, "errors": errors, "total": total}
