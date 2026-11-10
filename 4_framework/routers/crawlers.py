"""
크롤러 제어 라우터
- /api/com2usn  com2usN YouTube 드라마 크롤링
- /api/mbccb    MBC충북 뉴스/VOD/LIVE 크롤링
- /api/cjenm    CJ ENM BWC 동기화
- /api/crawl    전체 크롤링
- /api/reset    DB 초기화
"""

import logging
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel

from core.database import (
    upsert_drama, get_news_articles,
    upsert_news_article, get_existing_news_urls,
    upsert_youtube_video, get_youtube_videos, get_existing_youtube_video_ids,
    get_existing_youtube_page_urls, apply_news_edit, apply_video_edit, get_mbccb_all,
)
from core.browser import get_context
from core.paths import DATA_DIR
from core.progress import make_progress, update_progress, finish_progress, get_all_progress

router = APIRouter()


# ════════════════════════════════════════════════════════════════
# com2usN
# ════════════════════════════════════════════════════════════════

def _com2usn_to_drama_data(v: dict) -> dict:
    """com2usN youtube_videos 레코드 → upsert_drama() 형식으로 변환."""
    import re as _re
    wiki       = v.get("wiki_data") or {}
    video_id   = v.get("video_id") or ""
    youtube_url = v.get("youtube_url") or ""
    page_url   = v.get("page_url") or ""

    # 포스터: 저장된 poster > wiki_poster > YouTube 썸네일
    poster = v.get("poster") or wiki.get("wiki_poster") or ""
    if not poster:
        vid = video_id if video_id and not video_id.startswith("wiki_") else ""
        if not vid and youtube_url:
            _m = _re.search(r"v=([A-Za-z0-9_-]{11})", youtube_url)
            if _m:
                vid = _m.group(1)
        if vid:
            poster = f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg"

    drama_title = v.get("drama_title") or v.get("title") or ""

    # 방영 기간
    air_date_raw = wiki.get("air_date") or ""
    _period: dict = {}
    if air_date_raw:
        # com2usN 작품 페이지는 시즌 하나에 대응하므로, 위키에 시즌별 기간이 이어 적혀 있으면 해당 시즌(시즌 미표기=1) 기간을 쓴다
        from services.work_matcher import split_season as _ss, season_period as _sp
        _picked = _sp(air_date_raw, _ss(drama_title)[1] or 1, first_ok=True)
    if air_date_raw and _picked:
        _period = _picked
    elif air_date_raw:
        _parts = _re.split(r"[~\-~]", air_date_raw)
        _period = {
            "start": _parts[0].strip() if _parts else "",
            "end":   _parts[1].strip() if len(_parts) > 1 else "",
            "raw":   air_date_raw,
        }

    # 에피소드 수
    _ep_str = wiki.get("episodes") or ""
    _ep_count = None
    if _ep_str:
        # '시즌 1 : 10부작' 처럼 시즌 번호가 앞에 오면 첫 숫자가 회차 수가 아니므로 'N부작/회/화' 를 우선
        _ep_m = _re.search(r"(\d+)\s*(?:부작|회|화|편)", _ep_str) or _re.search(r"\d+", _ep_str)
        if _ep_m:
            _ep_count = int(_ep_m.group(1) if _ep_m.re.groups else _ep_m.group())

    return {
        "title":             drama_title,
        "broadcaster":       "com2usN",
        "content_type":      "drama",
        "official_site_url": page_url,  # com2usN 드라마 페이지
        "wikipedia": {
            "wiki_url":       wiki.get("wiki_url") or "",
            "wiki_poster":    wiki.get("wiki_poster") or "",
            "summary":        wiki.get("summary") or "",
            "infobox":        wiki.get("infobox") or {},
            "external_links": wiki.get("external_links") or [],
            "sections":       wiki.get("sections") or [],   # 위키피디아 전체 본문 섹션
            "no_wiki":        wiki.get("no_wiki") or False,
        },
        "metadata": {
            "poster_url":       poster,
            "description":      wiki.get("summary") or v.get("description") or "",
            "channel":          wiki.get("channel") or "com2usN",
            "cast":             wiki.get("cast") or [],
            "director":         wiki.get("director") or [],
            "writer":           wiki.get("writer") or [],
            "genre":            wiki.get("genre") or "",
            "broadcast_period": _period,
            "episode_count":    _ep_count,
            "extra": {"source": "com2usn", "youtube_url": youtube_url},   # 출처별 고유 필드
        },
    }


class Com2usNCrawlRequest(BaseModel):
    max_count: int = 0   # 0 = 전체


@router.post("/api/com2usn/crawl")
async def crawl_com2usn(req: Com2usNCrawlRequest, background_tasks: BackgroundTasks):
    """com2usN 공식 홈페이지에서 드라마 YouTube 영상을 크롤링합니다."""
    if not await get_context():
        raise HTTPException(status_code=503, detail="브라우저가 준비되지 않았습니다.")

    async def _job():
        from crawlers.com2usn import crawl_com2usn_all
        make_progress("com2usn", "com2usN 드라마")
        def _cb(cur, tot, msg): update_progress("com2usn", cur, tot, msg)
        existing_ids = await get_existing_youtube_video_ids(source="com2usn")
        ctx = await get_context()
        if not ctx:
            finish_progress("com2usn", 0)
            return
        existing_pages = await get_existing_youtube_page_urls(source="com2usn", category="drama")
        videos = await crawl_com2usn_all(ctx, max_count=req.max_count, on_progress=_cb,
                                         existing_video_ids=existing_ids, existing_page_urls=existing_pages)
        saved = 0
        for v in videos:
            try:
                _, is_new = await upsert_youtube_video(v)
                _did, _ = await upsert_drama(_com2usn_to_drama_data(v))
                if is_new:
                    saved += 1
                try:      # 같은 작품이 TVING / tvN 에 있으면 연결해 줄거리·출연·회차를 보충
                    from services import cjenm_pair
                    await cjenm_pair.complete_saved(int(_did), force=True)
                except Exception as _e:
                    logging.warning(f"com2usN TVING/tvN 연결 실패({v.get('drama_title')}): {_e}")
            except Exception as e:
                logging.warning(f"com2usN 영상 저장 실패: {e}")
        finish_progress("com2usn", saved)

    background_tasks.add_task(_job)
    _label = "전체 수집" if req.max_count <= 0 else f"최대 {req.max_count}개"
    return {"message": f"com2usN 드라마 크롤링 시작 ({_label})"}


@router.get("/api/com2usn/videos")
async def list_com2usn_videos(
    q: str = Query("", description="검색어"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    items, total = await get_youtube_videos(source="com2usn", q=q, limit=limit, offset=offset)
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.post("/api/com2usn/migrate-to-library")
async def migrate_com2usn_to_library():
    """youtube_videos(com2usn)에 저장된 데이터를 dramas 테이블로 마이그레이션."""
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    saved = 0
    async with _aio.connect(str(_DB_PATH)) as db:
        db.row_factory = _aio.Row
        cur = await db.execute("SELECT * FROM youtube_videos WHERE source='com2usn'")
        rows = await cur.fetchall()
    for row in rows:
        v = dict(row)
        wd = v.get("wiki_data")
        if isinstance(wd, str):
            try:
                v["wiki_data"] = json.loads(wd)
            except Exception:
                v["wiki_data"] = {}
        try:
            _did, _ = await upsert_drama(_com2usn_to_drama_data(v))
            saved += 1
            try:
                from services import cjenm_pair
                await cjenm_pair.complete_saved(int(_did))
            except Exception as _e:
                logging.warning(f"com2usN TVING/tvN 연결 실패({v.get('drama_title')}): {_e}")
        except Exception as e:
            logging.warning(f"migrate 실패 ({v.get('drama_title')}): {e}")
    return {"migrated": saved}


@router.delete("/api/reset/com2usn")
async def reset_com2usn():
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    async with _aio.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM youtube_videos WHERE source='com2usn'")
        await db.commit()
        cur = await db.execute("SELECT changes()")
        row = await cur.fetchone()
    return {"message": "com2usN DB 초기화 완료", "deleted": row[0] if row else 0}


# ════════════════════════════════════════════════════════════════
# MBC충북
# ════════════════════════════════════════════════════════════════

class MbccbNewsCrawlRequest(BaseModel):
    max_count: int = 0
    max_pages: int = 0
    max_articles: int = 0


@router.post("/api/mbccb/crawl/news")
async def crawl_mbccb_news_api(req: MbccbNewsCrawlRequest, background_tasks: BackgroundTasks):
    ctx = await get_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="브라우저가 준비되지 않았습니다.")
    effective = req.max_count or req.max_articles or 0

    # 이미 같은 크롤이 돌고 있으면 새로 시작하지 않는다 (같은 기사를 두 번씩 받아 서로 중복 작업하던 문제)
    if any(t.get("id") == "mbccb_news" and t.get("running") for t in get_all_progress()):
        return {"message": "MBC충북 뉴스 크롤링이 이미 진행 중입니다. 끝난 뒤 다시 시작해 주세요.", "already_running": True}
    make_progress("mbccb_news", "MBC충북 뉴스 기사")   # 요청 시점에 바로 '실행 중'으로 표시해 연속 요청도 막는다

    async def _job():
        from crawlers.mbccb import crawl_mbccb_news
        saved_ref = [0]
        def _cb(cur, tot, msg):
            # 총 개수를 모르는 단계(tot<=0)는 total=0 으로 넘겨 화면이 '진행 중(개수 미정)'으로 표시하게 한다
            update_progress("mbccb_news", cur, tot if tot and tot > 0 else 0, msg)
        existing_urls = await get_existing_news_urls(source="mbccb")

        async def _save(art):
            try:
                _, is_new = await upsert_news_article(art)
                if is_new:
                    saved_ref[0] += 1
                return is_new
            except Exception as e:
                logging.warning(f"MBC충북 기사 저장 실패: {e}")
                return False

        await crawl_mbccb_news(ctx, max_count=effective, on_progress=_cb,
                               existing_urls=existing_urls, on_save=_save)

        # 이미 저장된 기사 재확인: 영상이 발행 뒤에 임베드된 기사, 옆 기사 이미지가 섞인 기사를 바로잡는다
        try:
            from crawlers.mbccb import recheck_news_articles
            from core.database import get_news_urls_to_recheck
            recheck_urls = await get_news_urls_to_recheck("mbccb")
            if recheck_urls:
                await recheck_news_articles(recheck_urls, _save, on_progress=_cb)
        except Exception as e:
            logging.warning(f"MBC충북 기존 기사 재확인 실패: {e}")
        finish_progress("mbccb_news", saved_ref[0])

    async def _guarded_job():
        # 작업이 예외로 끝나도 '실행 중' 표시가 남아 다음 크롤이 영영 막히지 않게 마무리한다
        try:
            await _job()
        except Exception as e:
            logging.error(f"MBC충북 뉴스 크롤링 실패: {e}", exc_info=True)
        finally:
            if any(t.get("id") == "mbccb_news" and t.get("running") for t in get_all_progress()):
                finish_progress("mbccb_news", 0)

    background_tasks.add_task(_guarded_job)
    count_label = "전체 수집" if effective == 0 else f"최대 {effective}개"
    return {"message": f"MBC충북 뉴스 크롤링 시작 ({count_label})"}


@router.post("/api/mbccb/crawl/news/refetch-youtube")
async def refetch_mbccb_news_youtube(background_tasks: BackgroundTasks):
    ctx = await get_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="브라우저가 준비되지 않았습니다.")

    async def _job():
        import aiosqlite as _aio, asyncio as _aio2, httpx as _httpx, re as _re
        from bs4 import BeautifulSoup as _BS
        from core.database import DB_PATH as _DB_PATH
        make_progress("mbccb_refetch", "YouTube 재수집")
        async with _aio.connect(str(_DB_PATH)) as db:
            cur = await db.execute(
                "SELECT id, url FROM news_articles WHERE source='mbccb' AND (video_id IS NULL OR video_id='')"
            )
            rows = await cur.fetchall()
        total = len(rows)
        updated = 0
        yt_re = _re.compile(r'(?:youtube\.com/(?:embed|watch|v|shorts)/|youtu\.be/)([A-Za-z0-9_-]{11})')
        _headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        async with _httpx.AsyncClient(headers=_headers, timeout=15, follow_redirects=True) as client:
            for i, (art_id, url) in enumerate(rows):
                update_progress("mbccb_refetch", i, total, f"재수집 중... ({i}/{total})")
                try:
                    resp = await client.get(url)
                    soup = _BS(resp.text, "html.parser")
                    video_id = ""
                    for iframe in soup.find_all("iframe"):
                        m = yt_re.search(iframe.get("src", ""))
                        if m: video_id = m.group(1); break
                    if not video_id:
                        for a in soup.find_all("a", href=yt_re):
                            m = yt_re.search(a.get("href", ""))
                            if m: video_id = m.group(1); break
                    if video_id:
                        youtube_url = f"https://www.youtube.com/watch?v={video_id}"
                        async with _aio.connect(str(_DB_PATH)) as db:
                            await db.execute(
                                "UPDATE news_articles SET video_id=?, youtube_url=? WHERE id=?",
                                (video_id, youtube_url, art_id)
                            )
                            await db.commit()
                        updated += 1
                except Exception as e:
                    logging.warning(f"YouTube 재수집 실패 ({url}): {e}")
                await _aio2.sleep(0.2)
        finish_progress("mbccb_refetch", updated)

    background_tasks.add_task(_job)
    return {"message": "YouTube 없는 MBC충북 뉴스 재수집 시작"}


@router.post("/api/mbccb/crawl/news/refetch-images")
async def refetch_mbccb_news_images(background_tasks: BackgroundTasks):
    async def _job():
        import aiosqlite as _aio, asyncio as _aio2, httpx as _httpx
        from crawlers.mbccb import crawl_news_article as _crawl_article
        from core.database import DB_PATH as _DB_PATH
        make_progress("mbccb_refetch_img", "이미지 재수집")
        async with _aio.connect(str(_DB_PATH)) as db:
            cur = await db.execute("SELECT id, url, image_url FROM news_articles WHERE source='mbccb'")
            rows = await cur.fetchall()
        headers = {"User-Agent": "Mozilla/5.0"}
        total = len(rows)
        updated = 0
        async with _httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=10) as client:
            for i, (art_id, url, old_img) in enumerate(rows):
                update_progress("mbccb_refetch_img", i, total, f"이미지 확인 중... ({i}/{total})")
                try:
                    full_url = old_img if old_img and old_img.startswith("http") else (
                        "https://news.mbccb.co.kr" + old_img if old_img and old_img.startswith("/") else old_img or "")
                    needs_refetch = not old_img
                    if full_url:
                        try:
                            r = await client.head(full_url, timeout=5)
                            needs_refetch = r.status_code != 200
                        except Exception:
                            needs_refetch = True
                    if needs_refetch:
                        art = await _crawl_article(None, url)
                        new_img = art.get("image_url", "")
                        if new_img:
                            async with _aio.connect(str(_DB_PATH)) as db:
                                await db.execute("UPDATE news_articles SET image_url=? WHERE id=?", (new_img, art_id))
                                await db.commit()
                            updated += 1
                except Exception as e:
                    logging.warning(f"이미지 재수집 실패 ({url}): {e}")
                await _aio2.sleep(0.3)
        finish_progress("mbccb_refetch_img", updated)

    background_tasks.add_task(_job)
    return {"message": "MBC충북 뉴스 이미지 재수집 시작"}


@router.get("/api/mbccb/news")
async def list_mbccb_news(
    q: str = Query("", description="검색어"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    edited: bool = Query(False, description="수동 수정된 기사만"),
):
    items, total = await get_news_articles(q=q, source="mbccb", limit=limit, offset=offset, edited=edited)
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.put("/api/mbccb/news/{article_id}")
async def update_news_article(article_id: int, body: dict):
    """뉴스 수정: 제목/요약/본문/이미지/기자/발행일. 바뀐 항목은 재수집에서 보호되고 수정 이력에 남는다."""
    try:
        changed = await apply_news_edit(article_id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "changed": changed}


@router.post("/api/mbccb/news/{article_id}/edit-log/revert")
async def revert_news_edit_api(article_id: int, body: dict):
    """뉴스 수정 이력의 한 줄을 '수정하기 전 값'으로 되돌린다. body: {index, field}"""
    from core.database import revert_news_edit
    try:
        label = await revert_news_edit(article_id, body.get("index"), str(body.get("field") or ""))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "label": label}


@router.post("/api/mbccb/news/{article_id}/reset-auto")
async def reset_news_auto_api(article_id: int, body: dict):
    """직접 수정한 뉴스 항목을 자동 값으로 복원. body: {fields: [..]} (생략 = 수정한 항목 전부)"""
    from core.database import reset_news_to_auto
    fields = body.get("fields") if isinstance(body.get("fields"), list) else None
    try:
        res = await reset_news_to_auto(article_id, fields)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **res}


@router.delete("/api/mbccb/news/{article_id}")
async def delete_news_article(article_id: int):
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    async with _aio.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM news_articles WHERE id=?", (article_id,))
        await db.commit()
    return {"ok": True, "deleted_id": article_id}


class BulkDeleteNewsRequest(BaseModel):
    ids: list[int]

@router.post("/api/mbccb/news/bulk-delete")
async def bulk_delete_news(req: BulkDeleteNewsRequest):
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    if not req.ids:
        raise HTTPException(status_code=400, detail="삭제할 ID가 없습니다.")
    ph = ",".join("?" for _ in req.ids)
    async with _aio.connect(_DB_PATH) as db:
        await db.execute(f"DELETE FROM news_articles WHERE id IN ({ph})", req.ids)
        await db.commit()
    return {"ok": True, "deleted_count": len(req.ids)}


@router.post("/api/reset/news")
async def reset_news_db():
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    async with _aio.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM news_articles")
        await db.commit()
    return {"ok": True, "message": "뉴스 DB가 초기화되었습니다."}


class MbccbVodCrawlRequest(BaseModel):
    max_count: int = 300
    max_pages: int = 0


@router.post("/api/mbccb/crawl/vod")
async def crawl_mbccb_vod_api(req: MbccbVodCrawlRequest, background_tasks: BackgroundTasks):
    ctx = await get_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="브라우저가 준비되지 않았습니다.")
    effective = req.max_count or 300

    async def _job():
        from crawlers.mbccb import crawl_mbccb_vod
        make_progress("mbccb_vod", "MBC충북 다시보기")
        def _cb(cur, tot, msg): update_progress("mbccb_vod", cur, tot, msg)
        existing_ids = await get_existing_youtube_video_ids(source="mbccb")
        existing_pages = await get_existing_youtube_page_urls(source="mbccb", category="vod")
        videos = await crawl_mbccb_vod(ctx, max_count=effective, on_progress=_cb,
                                       existing_video_ids=existing_ids, existing_page_urls=existing_pages)
        saved = 0
        for v in videos:
            try:
                _, is_new = await upsert_youtube_video(v)
                if is_new:
                    saved += 1
            except Exception as e:
                logging.warning(f"MBC충북 VOD 저장 실패: {e}")
        finish_progress("mbccb_vod", saved)

    background_tasks.add_task(_job)
    return {"message": f"MBC충북 다시보기 크롤링 시작 (최대 {effective}개)"}


class MbccbLiveCrawlRequest(BaseModel):
    max_count: int = 50


@router.post("/api/mbccb/crawl/live")
async def crawl_mbccb_live_api(req: MbccbLiveCrawlRequest, background_tasks: BackgroundTasks):
    ctx = await get_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="브라우저가 준비되지 않았습니다.")

    async def _job():
        from crawlers.mbccb import get_youtube_channel_live
        make_progress("mbccb_live", "MBC충북 뉴스 LIVE")
        def _cb(cur, tot, msg): update_progress("mbccb_live", cur, tot, msg)
        existing_ids = await get_existing_youtube_video_ids(source="mbccb")
        videos = await get_youtube_channel_live(ctx, max_count=req.max_count,
                                                on_progress=_cb, existing_video_ids=existing_ids)
        saved = 0
        for v in videos:
            v["source"] = "mbccb"
            v["category"] = "live"
            if v.get("video_id"):
                try:
                    _, is_new = await upsert_youtube_video(v)
                    if is_new:
                        saved += 1
                except Exception as e:
                    logging.warning(f"MBC충북 LIVE 저장 실패: {e}")
        finish_progress("mbccb_live", saved)

    background_tasks.add_task(_job)
    return {"message": f"MBC충북 뉴스 LIVE 크롤링 시작 (최대 {req.max_count}개)"}


@router.get("/api/mbccb/videos")
async def list_mbccb_videos(
    category: str = Query("", description="카테고리 (vod/live)"),
    q: str = Query("", description="검색어"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    edited: bool = Query(False, description="수동 수정된 영상만"),
):
    items, total = await get_youtube_videos(source="mbccb", category=category, q=q,
                                            limit=limit, offset=offset, edited=edited)
    return {"total": total, "offset": offset, "limit": limit, "items": items}


@router.get("/api/mbccb/all")
async def list_mbccb_all(
    q: str = Query("", description="검색어"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    edited: bool = Query(False, description="수동 수정된 항목만"),
):
    """전체보기: 뉴스 → 다시보기 → LIVE 를 이어 붙인 목록 (페이지 이동 지원)."""
    return await get_mbccb_all(q=q, limit=limit, offset=offset, edited=edited)


class BulkDeleteVideosRequest(BaseModel):
    ids: list[int]


@router.post("/api/mbccb/videos/bulk-delete")
async def bulk_delete_mbccb_videos(req: BulkDeleteVideosRequest):
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    if not req.ids:
        raise HTTPException(status_code=400, detail="삭제할 ID가 없습니다.")
    ph = ",".join("?" for _ in req.ids)
    async with _aio.connect(_DB_PATH) as db:
        cur = await db.execute(f"DELETE FROM youtube_videos WHERE source='mbccb' AND id IN ({ph})", req.ids)
        deleted = cur.rowcount
        await db.commit()
    return {"ok": True, "deleted_count": deleted}


@router.post("/api/reset/mbccb-videos/{category}")
async def reset_mbccb_videos(category: str):
    """MBC충북 다시보기(vod) 또는 뉴스LIVE(live) 전체 삭제."""
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    if category not in ("vod", "live"):
        raise HTTPException(status_code=400, detail="category 는 vod 또는 live 여야 합니다.")
    label = "다시보기" if category == "vod" else "뉴스LIVE"
    async with _aio.connect(_DB_PATH) as db:
        cur = await db.execute("DELETE FROM youtube_videos WHERE source='mbccb' AND category=?", (category,))
        deleted = cur.rowcount
        await db.commit()
    return {"ok": True, "deleted": deleted, "message": f"{label} {deleted}개가 삭제되었습니다."}


@router.put("/api/mbccb/videos/{row_id}")
async def update_mbccb_video(row_id: int, body: dict):
    """다시보기/LIVE 수정: 제목/목록 제목/설명. 바뀐 항목은 재수집에서 보호되고 수정 이력에 남는다."""
    try:
        changed = await apply_video_edit(row_id, body)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    items, _ = await get_youtube_videos(source="mbccb", row_id=row_id, limit=1)
    changed = [{"description": "body", "drama_title": "list_title"}.get(f, f) for f in changed]   # 공통 키 이름으로
    return {"ok": True, "changed": changed, "item": items[0] if items else None}


@router.post("/api/mbccb/search")
async def search_mbccb(body: dict):
    q = body.get("query", "")
    t = body.get("type", "news")
    if t == "news":
        items, total = await get_news_articles(q=q, source="mbccb", limit=20)
        return {"type": "news", "total": total, "items": items}
    else:
        items, total = await get_youtube_videos(source="mbccb",
            category=t if t in ("vod", "live") else "", q=q, limit=20)
        return {"type": t, "total": total, "items": items}


@router.delete("/api/reset/mbccb")
async def reset_mbccb():
    import aiosqlite as _aio
    from core.database import DB_PATH as _DB_PATH
    async with _aio.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM news_articles")
        await db.execute("DELETE FROM youtube_videos WHERE source='mbccb'")
        await db.commit()
        cur = await db.execute("SELECT changes()")
        row = await cur.fetchone()
    return {"message": "MBC충북 DB 초기화 완료", "deleted": row[0] if row else 0}


# ════════════════════════════════════════════════════════════════
# CJ ENM (BWC 동기화)
# ════════════════════════════════════════════════════════════════

class CjenmSyncRequest(BaseModel):
    wiki_enrich: bool = True       # Wikipedia 후처리 여부
    collection_name: str = ""      # 특정 컬렉션만 동기화 (빈 문자열 = 전체)


@router.post("/api/cjenm/sync-to-library")
async def sync_cjenm_to_library(req: CjenmSyncRequest, background_tasks: BackgroundTasks):
    """CJ ENM BWC 수집 데이터를 dramas 라이브러리로 동기화합니다.
    collection_name이 있으면 해당 컬렉션만, 없으면 전체 스캔.
    wiki_enrich=True이면 Wikipedia 후처리 수행.
    """


    async def _run():
        from services.bwc_sync import sync_collection_to_library
        make_progress("cjenm_sync", "CJ ENM 라이브러리 동기화")

        def _cb(cur, tot, msg):
            update_progress("cjenm_sync", cur, tot, msg)

        if req.collection_name:
            # 특정 컬렉션만 동기화 (크롤 직후 자동 호출)
            result = await sync_collection_to_library(
                collection_name=req.collection_name,
                wiki_enrich=req.wiki_enrich,
                on_progress=_cb,
            )
        else:
            # 수동 동기화: 최신 컬렉션만 동기화 (전체 스캔 방지)
            from services.bwc_sync import scan_bwc_collections, latest_collection
            all_files = scan_bwc_collections()
            latest_col = latest_collection(set(f["collection_name"] for f in all_files))
            if latest_col:
                # 컬렉션 이름(날짜순) 기준으로 최신 컬렉션 선택
                update_progress("cjenm_sync", 0, 1, f"최신 컬렉션 동기화: {latest_col}")
                result = await sync_collection_to_library(
                    collection_name=latest_col,
                    wiki_enrich=req.wiki_enrich,
                    on_progress=_cb,
                )
            else:
                result = {"saved": 0, "skipped": 0, "errors": 0, "total": 0}

        finish_progress("cjenm_sync", result["saved"])
        logging.info(f"[CJ ENM] 동기화 완료: {result}")

    async def _job():
        try:
            await _run()
        except Exception as e:      # 예외로 끝나도 진행상황이 '진행 중'으로 남지 않게 한다
            logging.exception(f"[CJ ENM] 동기화 실패: {e}")
            finish_progress("cjenm_sync", 0)

    background_tasks.add_task(_job)
    label = f"컬렉션 '{req.collection_name}'" if req.collection_name else "전체"
    wiki_label = " + Wikipedia" if req.wiki_enrich else ""
    return {"message": f"CJ ENM → 라이브러리 동기화 시작 ({label}{wiki_label})"}


@router.get("/api/cjenm/status")
async def cjenm_status():
    """CJ ENM BWC 수집 파일 현황 반환."""
    from services.bwc_sync import scan_bwc_collections
    files = scan_bwc_collections()
    # 컬렉션별 파일 수 집계
    col_counts: dict[str, int] = {}
    for f in files:
        col = f["collection_name"]
        col_counts[col] = col_counts.get(col, 0) + 1
    from services.bwc_sync import latest_collection
    latest = latest_collection(col_counts.keys())
    return {
        "total_files":   len(files),
        "latest_collection": latest,
        "collections":   col_counts,
    }


class CjenmWikiEnrichRequest(BaseModel):
    max_items: int = 100      # 한 번에 처리할 최대 개수 (0=전체)
    broadcaster: str = "CJ ENM"
    delay_sec: float = 0.8   # API 요청 간 딜레이 (초)


@router.post("/api/cjenm/enrich-wikipedia")
async def enrich_cjenm_wikipedia(req: CjenmWikiEnrichRequest, background_tasks: BackgroundTasks):
    """dramas 테이블에서 위키피디아 정보가 없는 CJ ENM 항목을 보충합니다.
    max_items=0 이면 전체 처리 (느림), max_items=N 이면 최신 N개만 처리.
    """

    async def _job():
        from services.bwc_sync import enrich_library_with_wikipedia
        label = f"위키피디아 보충 ({req.broadcaster})"
        make_progress("wiki_enrich", label)

        def _cb(cur, tot, msg):
            update_progress("wiki_enrich", cur, tot if tot > 0 else cur + 1, msg)

        result = await enrich_library_with_wikipedia(
            broadcaster=req.broadcaster,
            max_items=req.max_items,
            on_progress=_cb,
            delay_sec=req.delay_sec,
        )
        finish_progress("wiki_enrich", result["enriched"])
        logging.info(f"[wiki_enrich] 완료: {result}")

    background_tasks.add_task(_job)
    max_label = "전체" if req.max_items == 0 else f"최대 {req.max_items}개"
    return {"message": f"위키피디아 보충 시작 ({req.broadcaster}, {max_label})"}


@router.delete("/api/reset/cjenm")
async def reset_cjenm():
    """CJ ENM 수집 파일(data/bwc) 전체 삭제."""
    import shutil as _shutil
    bwc_dir = DATA_DIR / "bwc"
    count = 0
    if bwc_dir.exists():
        count = sum(1 for _ in bwc_dir.rglob("contents.json"))
        _shutil.rmtree(bwc_dir, ignore_errors=True)
        bwc_dir.mkdir(parents=True, exist_ok=True)
    return {"message": "CJ ENM 수집 데이터 초기화 완료", "deleted": count}


# ════════════════════════════════════════════════════════════════
# 전체 크롤링
# ════════════════════════════════════════════════════════════════

@router.post("/api/crawl/all-targets")
async def crawl_all_targets(background_tasks: BackgroundTasks):
    ctx = await get_context()
    if not ctx:
        raise HTTPException(status_code=503, detail="브라우저가 준비되지 않았습니다.")

    async def _job():
        from crawlers.com2usn import crawl_com2usn_all
        from crawlers.mbccb import crawl_mbccb_news, crawl_mbccb_vod, get_youtube_channel_live

        # 진행 추적에 등록해야 프론트가 완료를 감지해 목록을 자동 새로고침한다
        make_progress("crawl_all", "전체 크롤링 (com2usN + MBC충북)")
        total_saved = 0
        try:
            logging.info("[전체] com2usN 드라마 크롤링 시작")
            update_progress("crawl_all", 0, 4, "com2usN 드라마 수집 중...")
            videos = await crawl_com2usn_all(
                ctx, max_count=300,
                existing_video_ids=await get_existing_youtube_video_ids(source="com2usn"),
                existing_page_urls=await get_existing_youtube_page_urls(source="com2usn", category="drama"))
            for v in videos:
                try:
                    await upsert_youtube_video(v)
                    total_saved += 1
                except Exception:
                    pass

            logging.info("[전체] MBC충북 뉴스 크롤링 시작")
            update_progress("crawl_all", 1, 4, "MBC충북 뉴스 수집 중...")
            articles = await crawl_mbccb_news(ctx, max_count=300, existing_urls=await get_existing_news_urls(source="mbccb"))
            for a in articles:
                try:
                    await upsert_news_article(a)
                    total_saved += 1
                except Exception:
                    pass

            logging.info("[전체] MBC충북 다시보기 크롤링 시작")
            update_progress("crawl_all", 2, 4, "MBC충북 다시보기 수집 중...")
            vods = await crawl_mbccb_vod(
                ctx, max_count=300,
                existing_video_ids=await get_existing_youtube_video_ids(source="mbccb"),
                existing_page_urls=await get_existing_youtube_page_urls(source="mbccb", category="vod"))
            for v in vods:
                try:
                    await upsert_youtube_video(v)
                    total_saved += 1
                except Exception:
                    pass

            logging.info("[전체] MBC충북 LIVE 크롤링 시작")
            update_progress("crawl_all", 3, 4, "MBC충북 LIVE 수집 중...")
            lives = await get_youtube_channel_live(
                ctx, max_count=300, existing_video_ids=await get_existing_youtube_video_ids(source="mbccb"))
            for v in lives:
                v.setdefault("source", "mbccb")
                v.setdefault("category", "live")
                if v.get("video_id"):
                    try:
                        await upsert_youtube_video(v)
                        total_saved += 1
                    except Exception:
                        pass

            logging.info("[전체] 크롤링 완료")
        finally:
            finish_progress("crawl_all", total_saved)   # 오류가 나도 '완료'로 표시해 화면이 멈추지 않게 한다

    background_tasks.add_task(_job)
    return {"message": "com2usN + MBC충북 전체 크롤링 시작"}


@router.delete("/api/reset/library")
async def reset_library():
    import aiosqlite as _aiosqlite
    from core.database import DB_PATH as _DB_PATH
    async with _aiosqlite.connect(_DB_PATH) as db:
        cur = await db.execute("DELETE FROM dramas")
        deleted = cur.rowcount            # dramas 삭제 건수 (마지막 DELETE 의 changes() 는 crawl_queue 건수라 쓰지 않는다)
        for _t in ("drama_sources", "drama_people", "drama_episodes"):      # 작품에 딸린 출처·인물·회차도 같이 비운다
            try:
                await db.execute(f"DELETE FROM {_t}")
            except Exception:
                pass
        await db.execute("DELETE FROM crawl_queue")
        await db.commit()
    return {"message": "컨텐츠 라이브러리 초기화 완료", "deleted": deleted}
