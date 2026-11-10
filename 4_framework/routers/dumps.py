"""
위키 Dumps 업데이트 API

  GET  /api/dump/overview        최종 업데이트 날짜 + 덤프/인덱스 정보 + 진행상황
  GET  /api/dump/check           배포처(dumps.wikimedia.org) 최신 버전과 비교 → 최신 여부
  POST /api/dump/update          업데이트 시작 (?force=true 면 최신이어도 다시 인덱싱)
  POST /api/dump/update/cancel   진행 중인 업데이트 취소
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query

from services import dump_updater

router = APIRouter()


@router.get("/api/dump/overview")
async def dump_overview():
    return dump_updater.get_overview()


@router.get("/api/dump/check")
async def dump_check(refresh: bool = Query(False, description="캐시를 무시하고 위키 서버에 다시 확인")):
    """덤프 배포처의 최신 버전과 비교해 최신 여부를 돌려준다 (HEAD 요청 한 번)."""
    return await asyncio.to_thread(dump_updater.check_latest, refresh)


@router.post("/api/dump/update")
async def dump_update(force: bool = Query(False, description="최신이어도 강제로 다시 인덱싱")):
    try:
        state = dump_updater.start_update(force=force)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"ok": True, "update": state}


@router.post("/api/dump/update/cancel")
async def dump_update_cancel():
    return {"ok": dump_updater.cancel_update()}
