"""
드라마 라이브러리 라우터
- /api/search       단건 수동 검색
- /api/dramas       목록/상세/수정/삭제
- /api/dump-status  Dumps 인덱스 상태
- /api/broadcasters 방송사 목록
- /api/health       헬스체크
- /api/crawl/status DB 통계
- /api/crawl/progress 크롤링 진행상황
"""

import logging
import re
import urllib.parse
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.database import upsert_drama, get_dramas, get_drama_by_id, find_drama_id_by_wiki_url, get_stats, infer_content_type, normalize_drama_meta
from core.browser import get_context
from core.source_tables import delete_for_dramas
from core.progress import get_all_progress
from core.paths import DB_PATH
import aiosqlite
from crawlers.metadata import collect_drama_metadata_with_context, WIKI_BROADCASTER_CHANNELS

router = APIRouter()




def _title_key(s: str) -> str:
    """제목 비교용 정규화: 공백 제거 + 소문자."""
    return re.sub(r"\s+", "", s or "").lower()


def _title_parts(title: str) -> list[str]:
    """DB 제목에서 비교 후보를 뽑는다.
    '쓸쓸하고 찬란하神 - 도깨비' → 전체 / '쓸쓸하고 찬란하神' / '도깨비'
    '도깨비 (드라마)'            → 전체 / '도깨비'
    시즌 번호는 조각 안에 그대로 남으므로 '신병' 이 '신병2' 와 같다고 보지 않는다."""
    t = (title or "").strip()
    parts = [t]
    parts += [p for p in re.split(r"\s+[-–—:]\s+|\s*[:：]\s*", t) if p]
    parts.append(re.sub(r"\s*[\(（\[][^\)）\]]*[\)）\]]\s*", " ", t).strip())  # 괄호 부가설명 제거
    return [p for p in parts if p]


def _filter_rows_by_origin(rows: list[dict], origin: str) -> list[dict]:
    """수동검색의 국내/해외 선택에 맞지 않는(제작 국가가 확실히 반대편인) 저장 작품을 뺀다. 미확인·추정은 남긴다."""
    if origin not in ("domestic", "foreign"):
        return rows
    out = []
    for d in rows:
        o = ((d.get("metadata_json") or {}).get("origin") or {}) if isinstance(d.get("metadata_json"), dict) else {}
        if o.get("group") in ("domestic", "foreign") and o["group"] != origin and o.get("confidence") != "low":
            continue
        out.append(d)
    return out


def _pick_existing_by_title(rows: list[dict], query: str) -> dict | None:
    """검색어와 같은 작품이 이미 DB 에 있으면 그 행을 반환 (전체 일치 → 제목 조각 일치 순)."""
    q = _title_key(query)
    if not q:
        return None
    exact = [r for r in rows if _title_key(r.get("title", "")) == q]
    if exact:
        return exact[0]
    # 조각 일치가 여러 작품이면 어느 쪽인지 알 수 없으므로 고르지 않고 새로 검색한다
    part = [r for r in rows if any(_title_key(p) == q for p in _title_parts(r.get("title", "")))]
    return part[0] if len(part) == 1 else None

# ── 허용 공식 사이트 도메인 화이트리스트 ──────────────────────────────────────
_ALLOWED_OFFICIAL_DOMAINS: frozenset[str] = frozenset({
    "tvn.cjenm.com", "cjenm.com",
    "tving.com", "www.tving.com", "program.tving.com", "tving.page.link",
    "news.mbccb.co.kr", "mbccb.co.kr",
    "kbs.co.kr", "program.kbs.co.kr",
    "imbc.com", "program.imbc.com", "dramainfo.imbc.com",
    "sbs.co.kr", "programs.sbs.co.kr",
    "jtbc.co.kr", "tv.jtbc.co.kr", "tv.jtbc.joins.com",
    "ocn.co.kr", "tvchosun.com",
    "ichannela.com", "channela.com",
    "mbn.co.kr",
    "ena.co.kr", "enachannel.com", "ktena.co.kr",
    "skylifetv.co.kr",
    "wavve.com", "www.wavve.com",
    "watcha.com", "www.watcha.com",
    "disneyplus.com", "www.disneyplus.com",
    "coupangplay.com", "seezn.com", "laftel.net",
    "interest.me", "tv.naver.com", "page.kakao.com",
    "studiodragontv.com", "studiodragon.net", "jcontentree.com",
    "com2usn.com", "com2us.com", "n.com2us.com",
    "youtube.com", "www.youtube.com",
    "ko.wikipedia.org", "wikipedia.org",
    "namu.wiki", "www.namu.wiki",
})


def _check_allowed_domain(url: str) -> bool:
    if not url:
        return True
    from urllib.parse import urlparse
    try:
        from crawlers.filters import _is_netflix_url
        if _is_netflix_url(url):          # Netflix 는 어떤 경로로도 허용하지 않는다
            return False
        # lstrip("www.") 는 문자 집합을 제거해 'wavve.com' → 'avve.com' 이 되므로 접두사만 제거
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
        for allowed in _ALLOWED_OFFICIAL_DOMAINS:
            if host == allowed or host.endswith("." + allowed):
                return True
        try:
            from crawlers.filters import _is_known_drama_domain
            if _is_known_drama_domain(url):
                return True
        except ImportError:
            pass
        return False
    except Exception:
        return False


# ── 수동 검색 ─────────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    title: str = ""
    wiki_only: bool = False
    direct_url: str = ""
    wiki_source: str = "dump"  # 항상 dump 고정 (API 검색 제거, 하위호환용 필드)
    origin: str = ""           # "domestic"(국내 드라마) / "foreign"(해외 드라마) / ""(전체). 제목 검색에만 적용된다


async def _db_search_payload(drama_id, wiki_source="dump", matched_by_wiki_url=False):
    """저장된 작품을 수동검색 응답 형식으로 만든다 (제목 일치 / 같은 위키 문서 일치 공용)."""
    _full = await get_drama_by_id(drama_id)
    if not _full:
        return None
    _full["wikipedia"] = {
        "title":          _full.get("title", ""),
        "wiki_url":       _full.get("wiki_url", ""),
        "wiki_poster":    _full.get("poster", ""),
        "summary":        _full.get("description", ""),
        "infobox":        _full.get("infobox_json") or {},
        "external_links": _full.get("external_links_json") or [],
        "categories":     [],
        # DB에 저장된 위키 섹션을 수동검색 결과에도 포함
        "sections":       _full.get("wiki_sections") or _full.get("wiki_sections_json") or [],
    }
    _full["official_site"] = {
        "poster":   _full.get("poster", ""),
        "og_image": (_full.get("og_json") or {}).get("image", ""),
        "title":    _full.get("title", ""),
    }
    _full["metadata"] = _full.get("metadata_json") or {}
    _full.setdefault("youtube_videos", [])
    _full["_from_db"] = True
    from core.source_tables import get_extras
    _full["extras"] = await get_extras(drama_id)      # 출처(tvN/TVING) · 인물 · 회차
    if matched_by_wiki_url:
        _full["_matched_by_wiki_url"] = True   # 검색어와 제목은 달라도 같은 위키 문서라 기존 항목을 보여줌
    _full["_wiki_source"] = wiki_source
    _full["wiki_only"] = False
    return _full


_MBCCB_KIND_LABEL = {"news": "뉴스", "vod": "다시보기", "live": "뉴스LIVE"}


async def _save_mbccb_link(ctx, url: str, kind: str) -> dict:
    """수동검색창에 넣은 MBC충북 링크를 종류(뉴스 기사 / 다시보기 / 뉴스LIVE)별 탭에 저장한다."""
    import aiosqlite
    from core.database import DB_PATH, upsert_news_article, upsert_youtube_video
    from crawlers.mbccb import collect_mbccb_link

    res = await collect_mbccb_link(ctx, url, kind)
    if not res.get("ok"):
        raise HTTPException(status_code=422, detail=res.get("error") or "링크를 수집하지 못했습니다.")
    d = res["data"]

    if kind == "news":
        _id, is_new = await upsert_news_article(d)
        item = {"id": _id, "title": d.get("title", ""), "url": d.get("url", ""),
                "image_url": d.get("image_url", ""), "published_at": d.get("published_at", "")}
        return {"mbccb_link": True, "kind": kind, "label": _MBCCB_KIND_LABEL[kind], "is_new": is_new, "item": item}

    # 같은 유튜브 영상이 이미 다른 탭(다시보기/뉴스LIVE)에 있으면 탭을 바꾸지 않는다 — 뉴스LIVE 링크로 다시보기가 덮이는 것 방지
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, category FROM youtube_videos WHERE video_id=?", (d["video_id"],))
        row = await cur.fetchone()
    if row and kind == "live" and row[1] != "live":
        kind_now = row[1] if row[1] in _MBCCB_KIND_LABEL else kind
        item = {"id": row[0], "title": d.get("title", ""), "url": d.get("youtube_url", ""),
                "image_url": f"https://img.youtube.com/vi/{d['video_id']}/mqdefault.jpg"}
        return {"mbccb_link": True, "kind": kind_now, "label": _MBCCB_KIND_LABEL.get(kind_now, kind_now),
                "is_new": False, "item": item, "note": f"이미 '{_MBCCB_KIND_LABEL.get(kind_now, kind_now)}' 탭에 저장된 영상입니다."}

    _id, is_new = await upsert_youtube_video(d)
    item = {"id": _id, "title": d.get("title", ""), "url": d.get("youtube_url", ""),
            "image_url": f"https://img.youtube.com/vi/{d['video_id']}/mqdefault.jpg"}
    return {"mbccb_link": True, "kind": kind, "label": _MBCCB_KIND_LABEL[kind], "is_new": is_new, "item": item}


async def _collect_pair(coro, strict: bool) -> dict | None:
    """cjenm_pair 수집 결과를 수동검색 응답으로 만든다. 못 찾았거나(None) 대상이 아니면 None → 호출한 쪽이 기존 흐름으로 이어간다.
    Netflix 는 400 으로 거절한다."""
    from services import cjenm_pair
    try:
        out = await coro
    except cjenm_pair.BlockedError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except cjenm_pair.PairError as e:
        if strict:
            raise HTTPException(status_code=422, detail=str(e))
        logging.info(f"[pair] 건너뜀: {e}")
        return None
    except Exception as e:
        logging.warning(f"[pair] 수집 실패, 기존 검색으로 진행: {e}")
        return None
    if out is None:
        return None
    if out.status == "conflict":
        raise HTTPException(status_code=409, detail="; ".join(out.notes) or "같은 제목의 다른 작품과 구분할 수 없습니다.")
    if out.status != "created" and out.status != "attached":
        return None
    payload = await _db_search_payload(out.drama_id)
    if not payload:
        return None
    payload["id"] = out.drama_id
    payload["_pair"] = out.summary()
    payload["_from_db"] = not out.is_new
    return payload


async def _topup_saved(drama_id) -> None:
    """저장된 작품에 TVING 만(또는 tvN 만) 있으면 상대편을 찾아 내용을 보충한다. 실패해도 검색은 그대로 진행."""
    if not drama_id:
        return
    from services import cjenm_pair
    try:
        await cjenm_pair.complete_saved(int(drama_id))
    except Exception as e:
        logging.warning(f"[pair] 저장본 보충 실패(id={drama_id}): {e}")


async def _find_saved_by_official_url(url: str):
    """official_site_url 이 같은 저장 작품 id (없으면 None). www·끝 슬래시 차이는 무시."""
    key = re.sub(r"^https?://(www\.)?", "", (url or "").strip()).rstrip("/").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, official_site_url FROM dramas WHERE official_site_url LIKE ?", ("%" + key,))
        for _id, u in await cur.fetchall():
            if re.sub(r"^https?://(www\.)?", "", (u or "").strip()).rstrip("/").lower() == key:
                return _id
    return None


@router.post("/api/search")
async def search_drama(req: SearchRequest):
    """단건 드라마 메타데이터 수집 (공식 사이트 → YouTube → Wikipedia 보충)"""
    ctx = await get_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="브라우저 컨텍스트가 준비되지 않았습니다.")

    direct_url   = (req.direct_url or "").strip()
    if direct_url and "://" not in direct_url and re.match(r"^(?:www\.|m\.)?[\w-]+(?:\.[\w-]+)+/", direct_url):
        direct_url = "https://" + direct_url          # 'tving.com/contents/P…' 처럼 scheme 없이 붙여넣은 주소
    drama_title  = (req.title or "").strip()
    wiki_url_param   = None
    preset_url_param = None

    # MBC충북 링크는 드라마가 아니라 뉴스 기사 / 다시보기 / 뉴스LIVE 중 맞는 탭 항목으로 저장한다
    if direct_url and "wikipedia.org/wiki/" not in direct_url:
        from crawlers.mbccb import classify_mbccb_link, is_mbccb_host
        _kind = classify_mbccb_link(direct_url)
        if _kind == "live" and drama_title:
            _kind = None          # 제목을 함께 넣은 유튜브 URL 은 기존처럼 드라마 수집 흐름으로 처리
        if _kind:
            return JSONResponse(await _save_mbccb_link(ctx, direct_url, _kind))
        if is_mbccb_host(direct_url):
            raise HTTPException(
                status_code=400,
                detail="MBC충북 링크는 뉴스 기사(menukey=61) 또는 다시보기(menukey=69) 상세 페이지 주소만 지원합니다.",
            )

    # com2usN 작품 URL: 페이지 자체에는 메타데이터가 없으므로(제목만 있음) 작품 제목을 알아낸 뒤 제목 검색 흐름으로 처리한다.
    #  1) 이미 저장된 작품이면 그대로 보여주고, 2) 아니면 목록·유튜브·위키 수집(제목 검색과 동일)으로 새로 만든다.
    if direct_url and "wikipedia.org/wiki/" not in direct_url:
        from crawlers.com2usn import is_com2usn_work_url, canon_com2usn_work_url, fetch_com2usn_work_title
        if is_com2usn_work_url(direct_url):
            _cu = canon_com2usn_work_url(direct_url)
            _saved = await _find_saved_by_official_url(_cu)
            if _saved:
                await _topup_saved(_saved)
                _full = await _db_search_payload(_saved, req.wiki_source)
                if _full:
                    return JSONResponse(_full)
            if not drama_title:
                drama_title = await fetch_com2usn_work_title(ctx, _cu)
                if not drama_title:
                    raise HTTPException(status_code=422, detail="com2usN 작품 페이지에서 제목을 읽지 못했습니다. 링크를 확인하거나 제목을 함께 입력해주세요.")
            req.title = drama_title
            direct_url = ""

    # 작품 코드가 없는 TVING 페이지(작품 전용 홍보관 /service/ip/… 등)는 페이지 안의 대표 작품(programCode)으로 바꿔서 처리한다
    if direct_url and "wikipedia.org/wiki/" not in direct_url:
        from services import cjenm_pair as _cp
        if _cp._tving_host(direct_url) and not _cp._tving_code_from_url(direct_url):
            _resolved = await _cp.resolve_tving_page(direct_url)
            if _resolved:
                logging.info(f"[search] TVING 페이지 {direct_url} → {_resolved}")
                direct_url = _resolved
            elif not drama_title:
                raise HTTPException(
                    status_code=422,
                    detail="이 TVING 주소에서 작품을 찾지 못했습니다. 작품 상세 주소(tving.com/contents/P…)를 넣거나 '제목' 란에 제목을 입력해주세요.")

    # tvN / TVING 작품 URL: 어느 쪽으로 검색하든 상대편(TVING ↔ tvN 공식 홈페이지)까지 찾아 한 작품으로 같이 저장한다.
    if direct_url and "wikipedia.org/wiki/" not in direct_url:
        from services import cjenm_pair
        if cjenm_pair.is_pair_url(direct_url):
            # TVING 링크는 제목 없이는 아래 일반 흐름으로 이어갈 수 없다(OTT URL 안내만 나옴). 수집이 실패하면 실제 이유(코드 없음·HTTP 오류 등)를 그대로 돌려준다.
            _strict = cjenm_pair._is_tving_url(direct_url) and not drama_title
            _pair = await _collect_pair(cjenm_pair.collect_from_url(direct_url), strict=_strict)
            if _pair:
                return JSONResponse(_pair)
            # 작품 상세 URL 이 아니면(예: 메인 페이지) 아래 기존 흐름으로 이어간다

    if direct_url:
        if "wikipedia.org/wiki/" in direct_url:
            wiki_url_param = direct_url
            if not drama_title:
                from urllib.parse import unquote
                drama_title = unquote(direct_url.rstrip("/").split("/wiki/")[-1].split("?")[0]).replace("_", " ")
        else:
            from urllib.parse import urlparse
            _host = urlparse(direct_url).netloc
            # Netflix 포함 비허용 도메인은 직접 입력 URL 로 받지 않는다 (_check_allowed_domain 이 Netflix 차단)
            if not _check_allowed_domain(direct_url):
                raise HTTPException(
                    status_code=400,
                    detail=f"'{_host}'은(는) 지원하지 않는 도메인입니다. "
                           "tvN, TVING, MBC충북, KBS, MBC, SBS, JTBC, 위키피디아 등 허용된 사이트 URL만 사용 가능합니다.",
                )
            preset_url_param = direct_url
            if not drama_title:
                drama_title = "URL 직접 수집"
            _OTT_HOSTS = ("disneyplus.com", "tving.com",
                          "wavve.com", "watcha.com", "coupangplay.com", "seezn.com")
            if any(h in direct_url.lower() for h in _OTT_HOSTS) and not req.title:
                raise HTTPException(
                    status_code=400,
                    detail="OTT URL은 자동 제목 추출이 어렵습니다. '제목' 란에 드라마 제목을 함께 입력해주세요.",
                )

    if not drama_title and not direct_url:
        raise HTTPException(status_code=400, detail="드라마 제목 또는 URL을 입력해주세요.")

    # 기존 DB 조회 (제목 검색 시)
    _db_fallback = None
    if drama_title and not direct_url and not req.wiki_only:
        # 검색어가 제목에 들어 있는 행만 가져온 뒤(전체 목록을 훑지 않음) 제목 일치 여부를 판정한다
        _rows, _ = await get_dramas(q=drama_title.strip(), limit=200)
        _existing = _pick_existing_by_title(_filter_rows_by_origin(_rows, req.origin), drama_title)
        if _existing:
            await _topup_saved(_existing.get("id"))
            _full = await _db_search_payload(_existing.get("id"), req.wiki_source)
            if _full:
                # tvN/TVING 작품인데 tvN·TVING 출처가 아직 없으면(예: 예전에 목록 수집만 된 행) 바로 돌려주지 않고
                # 아래 tvN↔TVING 짝 수집을 먼저 시도한다. 못 찾으면 이 저장본을 그대로 보여준다.
                _need_pair = (not ((_full.get("extras") or {}).get("sources"))
                              and re.search(r"tvn|tving|티빙", str(_full.get("channel") or ""), re.I))
                if not _need_pair:
                    return JSONResponse(_full)
                _db_fallback = _full

    # 제목 검색: tvN 공식 홈페이지 목록 / TVING 검색에서 같은 제목이 정확히 하나 나오면 두 곳을 모두 찾아 같이 만든다.
    # (없으면 아래 위키피디아 흐름으로 이어간다. wiki_only 여부와 상관없이 수행)
    if drama_title and not direct_url and drama_title != "URL 직접 수집":
        from services import cjenm_pair
        from core.source_tables import get_extras
        # 이미 tvN/TVING 출처까지 저장된 작품이면 네트워크를 다시 타지 않고 저장본을 보여준다
        _rows2, _ = await get_dramas(q=drama_title.strip(), limit=200)
        _ex = _pick_existing_by_title(_filter_rows_by_origin(_rows2, req.origin), drama_title)
        if _ex:
            await _topup_saved(_ex["id"])
        if _ex and (await get_extras(_ex["id"]))["sources"]:
            _full = await _db_search_payload(_ex["id"], req.wiki_source)
            if _full:
                return JSONResponse(_full)
        _pair = await _collect_pair(cjenm_pair.collect_by_title(drama_title, origin=req.origin), strict=False)
        if _pair:
            return JSONResponse(_pair)
        if _db_fallback:
            return JSONResponse(_db_fallback)

    # 위키 문서 선택은 CJ ENM / com2usN 자동수집과 같은 공용 resolver(wiki_resolver)가 담당.
    # Dumps 인덱스가 없으면 검증 기준이 없으므로 수동검색도 막는다.
    req.wiki_source = "dump"
    from wiki.resolver import dump_index_available
    if not dump_index_available():
        raise HTTPException(status_code=503, detail="Dumps 인덱스가 없습니다.")

    try:
        from wiki.parse import WikiRateLimitError as _WikiRateLimit
    except ImportError:
        _WikiRateLimit = None

    try:
        data = await collect_drama_metadata_with_context(
            ctx, drama_title,
            wiki_url=wiki_url_param,
            preset_official_url=preset_url_param,
            wiki_only=req.wiki_only,
            origin=req.origin if not wiki_url_param else "",   # 위키 URL 을 직접 준 경우는 그 문서를 그대로 쓴다
        )
    except Exception as _e:
        if _WikiRateLimit and isinstance(_e, _WikiRateLimit):
            raise HTTPException(status_code=429, detail="위키피디아 API 요청 제한(429). 잠시 후 다시 시도해주세요.")
        raise

    # 위키 본문은 resolver 결과(data["wikipedia"])를 그대로 사용 — 다른 문서와 섞지 않음
    data["_wiki_source"] = "dump"

    import re as _re
    wiki_page_title = data.get("wikipedia", {}).get("title", "")
    if wiki_page_title:
        clean_title = _re.sub(r"\s*\(.*?\)\s*$", "", wiki_page_title).strip()
        if clean_title:
            data["title"] = clean_title

    if preset_url_param and data.get("title") in ("URL 직접 수집", ""):
        _meta_title = (data.get("metadata", {}).get("title", "") or
                       data.get("official_site", {}).get("title", ""))
        if _meta_title:
            data["title"] = _meta_title
    data["wiki_only"] = req.wiki_only

    # 검색어와 제목이 달라도('신병1' / '신병') 위키 문서가 이미 저장된 작품과 같으면 같은 작품이다.
    # 새로 저장하거나 덮어쓰지 않고 기존 항목을 그대로 보여준다.
    # (공식 홈페이지 수집을 끈 wiki_only 검색 — 화면 기본값 — 에서도 똑같이 적용한다)
    _same_id = await find_drama_id_by_wiki_url((data.get("wikipedia") or {}).get("wiki_url"))
    if _same_id:
        _nk = lambda t: _re.sub(r"\s+", "", t or "")
        _row = await get_drama_by_id(_same_id)
        # 검색어와 저장된 제목이 다를 때만 '같은 위키 문서' 배지를 붙인다 (제목이 같으면 그냥 라이브러리 항목)
        _differs = bool(drama_title) and _nk((_row or {}).get("title")) != _nk(drama_title)
        _same = await _db_search_payload(_same_id, req.wiki_source, matched_by_wiki_url=_differs)
        if _same:
            return JSONResponse(_same)

    # YouTube 수집
    yt_videos = []
    wiki = data.get("wikipedia", {})

    def _attach_wiki(yt_info: dict) -> dict:
        yt_info["wiki_data"] = {
            "summary": wiki.get("summary", ""),
            "infobox": wiki.get("infobox", {}),
            "sections": wiki.get("sections", []),
            "wiki_url": wiki.get("wiki_url", ""),
        }
        return yt_info

    import re as _re2
    def _yt_title_relevant(yt_title: str, drama_title: str) -> bool:
        if not yt_title or not drama_title:
            return True
        def _tok(t: str) -> set:
            t = _re2.sub(r"[\s\W_]+", " ", t).lower().strip()
            return {w for w in t.split() if len(w) >= 2}
        dt = _tok(drama_title)
        yt = _tok(yt_title)
        if not dt:
            return True
        return len(dt & yt) > 0

    if not req.wiki_only:
        from crawlers.com2usn import get_youtube_info, find_com2usn_drama_by_title
        yt_ids = data.get("official_site", {}).get("youtube_ids", [])
        for vid_id in yt_ids[:5]:
            try:
                yt_info = await get_youtube_info(ctx, vid_id)
                if not _yt_title_relevant(yt_info.get("title", ""), data["title"]):
                    continue
                yt_info.update({"source": "search", "category": "drama",
                                "drama_title": data["title"],
                                "page_url": data.get("official_site_url", "")})
                yt_videos.append(_attach_wiki(yt_info))
            except Exception as e:
                logging.warning(f"YouTube 수집 실패 ({vid_id}): {e}")

        if not yt_videos:
            try:
                com2usn_result = await find_com2usn_drama_by_title(ctx, req.title or data["title"])
                if com2usn_result and _yt_title_relevant(com2usn_result.get("title", ""), data["title"]):
                    com2usn_result["source"] = "search"
                    yt_videos.append(_attach_wiki(com2usn_result))
            except Exception as e:
                logging.warning(f"com2usN 검색 실패: {e}")

    data["youtube_videos"] = yt_videos

    from crawlers.filters import is_logo_like_poster as _is_logo
    _httpx_img = data.pop("_httpx_og_image", "")
    if _httpx_img and not _is_logo(_httpx_img) and not (data.get("metadata", {}) or {}).get("poster_url"):
        data.setdefault("metadata", {})["poster_url"] = _httpx_img

    # 공식사이트 poster 보완
    _site_poster = (data.get("official_site", {}) or {}).get("poster", "")
    _official_url = data.get("official_site_url", "")
    if not _site_poster and _official_url:
        try:
            import httpx as _hx_post
            import re as _re_post
            from bs4 import BeautifulSoup as _BS_post
            _hdr_post = {"User-Agent": "Mozilla/5.0", "Accept-Language": "ko-KR,ko;q=0.9"}
            _CONTENT = _re_post.compile(
                r"/data/file/|/upload/|/files/drama|/relatedmaterial|/original/poster"
                r"|/original/thumb|/contents/drama|/img/drama|/board/.*\.(jpg|jpeg|png|webp)"
                r"|/poster/|_poster\.|poster_|thumb_|_thumb\.|/pcms/photogroup/|/pcms/character/",
                _re_post.IGNORECASE,
            )
            _SKIP = ["logo", "sns_", "/btn", "icon", "no_profile", "no_img",
                     "/icon", "32x32", "favicon", ".gif", "sitemng", "systemmng", "banner"]
            async with _hx_post.AsyncClient(headers=_hdr_post, follow_redirects=True, timeout=10) as _hc:
                _hr = await _hc.get(_official_url)
                _hs = _BS_post(_hr.text, "lxml")
                _og = _hs.find("meta", property="og:image")
                _cand = _og.get("content", "").strip() if _og else ""
                if _cand and _is_logo(_cand):
                    _cand = ""
                if not _cand:
                    for _it in _hs.select("img[src]"):
                        _src = _it.get("src", "")
                        if not _src: continue
                        if any(s in _src.lower() for s in _SKIP): continue
                        if _is_logo(_src): continue
                        if _CONTENT.search(_src):
                            _cand = _src if _src.startswith("http") else (
                                f"{urllib.parse.urlparse(_official_url).scheme}://"
                                f"{urllib.parse.urlparse(_official_url).netloc}{_src}"
                                if _src.startswith("/") else _src
                            )
                            break
                if _cand:
                    data.setdefault("official_site", {})["poster"] = _cand
                    if not (data.get("metadata") or {}).get("poster_url"):
                        data.setdefault("metadata", {})["poster_url"] = _cand
        except Exception as _ep:
            logging.debug(f"공식사이트 poster 보완 실패: {_ep}")

    # official_site_url 도메인 검증
    _collected_official = data.get("official_site_url", "")
    if _collected_official and not _check_allowed_domain(_collected_official):
        data["official_site_url"] = ""
        data["official_site"] = {}

    # YouTube 썸네일 폴백
    _wiki_poster  = (data.get("wikipedia", {}) or {}).get("wiki_poster", "")
    _site_poster2 = (data.get("official_site", {}) or {}).get("poster", "")
    _meta_poster  = (data.get("metadata", {}) or {}).get("poster_url", "")
    if not any([_wiki_poster, _site_poster2, _meta_poster]):
        _first_yt = next(
            (v for v in data.get("youtube_videos", [])
             if v.get("video_id") and not v["video_id"].startswith("wiki_")),
            None,
        )
        if _first_yt:
            _yt_thumb = f"https://img.youtube.com/vi/{_first_yt['video_id']}/mqdefault.jpg"
            data.setdefault("metadata", {})["poster_url"] = _yt_thumb
            data.setdefault("official_site", {}).setdefault("poster", _yt_thumb)

    # DB 저장
    _title_ok     = data.get("title", "") not in ("", "URL 직접 수집")
    _has_wiki     = bool(data.get("wikipedia", {}).get("infobox"))
    _has_official = bool(data.get("official_site_url"))
    drama_id      = None

    # 저장 대상은 드라마·예능뿐. 분류가 안 되는 문서(인물·영화 등)는 결과만 보여주고 저장하지 않는다.
    _ctype = data.get("content_type") or infer_content_type(data.get("wikipedia"))
    data["_save_skipped_reason"] = "" if _ctype in ("drama", "variety") else "드라마/예능으로 분류되지 않아 저장하지 않음"

    if _title_ok and (_has_wiki or _has_official) and _ctype in ("drama", "variety"):
        _final_title = data.get("title", "").strip()
        _dup_rows, _ = await get_dramas(q="", limit=300)
        _dup = next((r for r in _dup_rows
                     if (r.get("title") or "").strip() == _final_title
                     or (r.get("title") or "").replace(" ", "") == _final_title.replace(" ", "")), None)
        if _dup:
            drama_id = _dup.get("id")
        else:
            drama_id, _ = await upsert_drama(data)
            await _topup_saved(drama_id)     # com2usN 작품이면 같은 TVING/tvN 작품을 찾아 붙인다 (그 외는 아무것도 안 함)

    # 응답의 metadata 는 항상 컨텐츠 라이브러리에 저장된 것과 같은 통일 스키마로 돌려준다.
    # (수집 직후의 _build_metadata 원본은 wiki_status/manual_fields/edit_log/extra 등이 없어 저장본과 형식이 달랐다)
    _saved_meta = None
    if drama_id:
        _saved_row = await get_drama_by_id(drama_id)
        _saved_meta = (_saved_row or {}).get("metadata_json") or None
    data["metadata"] = _saved_meta or normalize_drama_meta(data.get("metadata"), data.get("title"))

    return {"id": drama_id, **data}


# ── Dumps 인덱스 상태 ─────────────────────────────────────────────────────────

@router.get("/api/dump-status")
async def dump_status():
    import sqlite3 as _sqlite3
    from core.paths import WIKI_INDEX as index_db, WIKI_DUMP as dump_file
    status = {
        "dump_file_exists":   dump_file.exists(),
        "dump_file_size_mb":  round(dump_file.stat().st_size / 1024 / 1024, 1) if dump_file.exists() else 0,
        "index_exists":       index_db.exists(),
        "drama_count":        0,
        "indexed_at":         None,
    }
    if index_db.exists():
        try:
            con = _sqlite3.connect(str(index_db))
            meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
            status["drama_count"] = int(meta.get("drama_count", 0))
            status["indexed_at"]  = meta.get("indexed_at")
            con.close()
        except Exception:
            pass
    return status


# ── 드라마 목록/상세/수정/삭제 ────────────────────────────────────────────────

@router.get("/api/dramas")
async def list_dramas(
    q: str = Query("", description="검색어"),
    channel: str = Query("", description="채널 필터"),
    status: str = Query("", description="상태 필터"),
    broadcaster: str = Query("", description="방송사 필터"),
    content_type: str = Query("", description="컨텐츠 유형"),
    origin: str = Query("", description="제작 국가: domestic(국내) / foreign(해외) / unknown(판별 불가) / review(확인 필요)"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    dramas, total = await get_dramas(
        q=q, channel=channel, status=status,
        broadcaster=broadcaster, content_type=content_type, origin=origin,
        limit=limit, offset=offset,
    )
    # 목록에는 무거운 전체 데이터(wikipedia·tving·tvn)를 싣지 않는다 (상세 /api/dramas/{id} 에서 전부 내려간다)
    for d in dramas:
        m = d.get("metadata_json")
        if isinstance(m, dict):
            d["metadata_json"] = {k: v for k, v in m.items() if k not in ("wikipedia", "tving", "tvn", "cast_roles")}
    return {"total": total, "offset": offset, "limit": limit, "items": dramas}


@router.get("/api/dramas/{drama_id}")
async def get_drama(drama_id: int):
    drama = await get_drama_by_id(drama_id)
    if not drama:
        raise HTTPException(status_code=404, detail="드라마를 찾을 수 없습니다.")
    from core.source_tables import get_extras
    drama["extras"] = await get_extras(drama_id)      # 출처(tvN/TVING) · 인물 · 회차
    return drama


@router.put("/api/dramas/{drama_id}")
async def update_drama(drama_id: int, body: dict):
    from core.database import apply_drama_edit
    try:
        updated = await apply_drama_edit(drama_id, body)
    except LookupError:
        raise HTTPException(status_code=404, detail="드라마를 찾을 수 없습니다.")
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=f"같은 제목의 작품이 이미 있습니다: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    drama = await get_drama_by_id(drama_id)
    return {"ok": True, "updated": updated, "drama": drama}


@router.post("/api/dramas/{drama_id}/edit-log/revert")
async def revert_drama_edit_api(drama_id: int, body: dict):
    """수정 이력의 한 줄을 '수정하기 전 값'으로 되돌린다. body: {index, field}"""
    from core.database import revert_drama_edit
    try:
        label = await revert_drama_edit(drama_id, body.get("index"), str(body.get("field") or ""))
    except LookupError:
        raise HTTPException(status_code=404, detail="드라마를 찾을 수 없습니다.")
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=f"같은 제목의 작품이 이미 있습니다: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "label": label, "drama": await get_drama_by_id(drama_id)}


@router.post("/api/dramas/{drama_id}/reset-auto")
async def reset_drama_auto_api(drama_id: int, body: dict):
    """직접 수정한 항목을 자동 값으로 복원. body: {fields: [..]} (생략/빈 목록 = 직접 수정한 항목 전부)"""
    from core.database import reset_drama_to_auto
    fields = body.get("fields") if isinstance(body.get("fields"), list) else None
    try:
        res = await reset_drama_to_auto(drama_id, fields)
    except LookupError:
        raise HTTPException(status_code=404, detail="드라마를 찾을 수 없습니다.")
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=f"같은 제목의 작품이 이미 있습니다: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **res, "drama": await get_drama_by_id(drama_id)}


@router.delete("/api/dramas/{drama_id}")
async def delete_drama(drama_id: int):
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    async with _aio.connect(_DB_PATH) as db:
        cur = await db.execute("SELECT title FROM dramas WHERE id=?", (drama_id,))
        row = await cur.fetchone()
        await db.execute("DELETE FROM dramas WHERE id=?", (drama_id,))
        await delete_for_dramas(db, [drama_id])
        if row:
            await db.execute("DELETE FROM youtube_videos WHERE drama_title=?", (row[0],))
        await db.commit()
    return {"ok": True, "deleted_id": drama_id}


class BulkDeleteRequest(BaseModel):
    ids: list[int]

@router.post("/api/dramas/bulk-delete")
async def bulk_delete_dramas(req: BulkDeleteRequest):
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    if not req.ids:
        raise HTTPException(status_code=400, detail="삭제할 ID가 없습니다.")
    ph = ",".join("?" for _ in req.ids)
    async with _aio.connect(_DB_PATH) as db:
        titles_cur = await db.execute(f"SELECT title FROM dramas WHERE id IN ({ph})", req.ids)
        titles = [r[0] for r in await titles_cur.fetchall()]
        await db.execute(f"DELETE FROM dramas WHERE id IN ({ph})", req.ids)
        await delete_for_dramas(db, req.ids)
        if titles:
            tph = ",".join("?" for _ in titles)
            await db.execute(f"DELETE FROM youtube_videos WHERE drama_title IN ({tph})", titles)
        await db.commit()
    return {"ok": True, "deleted_count": len(req.ids)}


@router.post("/api/reset/dramas")
async def reset_dramas_db():
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    async with _aio.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM dramas")
        for _t in ("drama_sources", "drama_people", "drama_episodes"):
            try:
                await db.execute(f"DELETE FROM {_t}")
            except Exception:
                pass
        # 드라마에 딸린 영상(com2usN)만 지운다. MBC충북 다시보기·LIVE(category vod/live)는 드라마 데이터가 아니다.
        await db.execute("DELETE FROM youtube_videos WHERE category='drama'")
        await db.commit()
    return {"ok": True, "message": "드라마/예능 DB가 초기화되었습니다."}


# ── 방송사 목록 ───────────────────────────────────────────────────────────────

@router.get("/api/broadcasters")
async def list_broadcasters():
    result = {}
    for key, channels in WIKI_BROADCASTER_CHANNELS.items():
        result[key] = {"channels": sorted(channels)}
    return result


# ── 헬스 체크 ─────────────────────────────────────────────────────────────────

@router.get("/api/health")
async def health():
    return {"status": "ok"}


@router.get("/api/crawl/status")
async def crawl_status():
    stats = await get_stats()
    return {
        "db":    {"total": stats["total"], "ok": stats["ok"],
                  "no_official": stats["no_official"], "errors": stats["errors"]},
        "queue": {"pending": stats["queue_pending"], "running": stats["queue_running"]},
    }


@router.get("/api/crawl/progress")
async def get_all_progress_api():
    return {"tasks": get_all_progress()}
