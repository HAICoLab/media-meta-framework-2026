"""로그 탭용 API: 서버 로그 조회 / 브라우저 오류 수집 / 초기화"""

import time
from collections import deque

from fastapi import APIRouter, Query
from pydantic import BaseModel

from services import log_buffer

router = APIRouter()

# 브라우저가 오류를 무한히 쏟아내도 서버가 영향받지 않도록 분당 건수 제한
_client_hits: "deque[float]" = deque()
_CLIENT_PER_MIN = 60


class ClientLog(BaseModel):
    level: str = "error"
    message: str = ""
    location: str = ""   # 오류가 난 파일:줄 (또는 호출한 URL)
    stack: str = ""
    page: str = ""       # 오류 당시 보고 있던 탭/주소


@router.get("/api/logs")
async def get_logs(
    after_id: int = Query(0, ge=0, description="이 id 보다 새로운 로그만 (0 이면 최근 limit 건)"),
    limit: int = Query(500, ge=1, le=3000),
    level: str = Query("", description="INFO / WARNING / ERROR (이 레벨 이상)"),
    source: str = Query("", description="server / http / client"),
    q: str = Query("", description="검색어"),
):
    return {
        "boot": log_buffer.BOOT_ID,
        "last_id": log_buffer.last_id(),
        "counts": log_buffer.counts(),
        "items": log_buffer.get_entries(after_id=after_id, limit=limit, level=level, source=source, q=q),
    }


@router.post("/api/logs/client")
async def post_client_log(body: ClientLog):
    now = time.time()
    while _client_hits and now - _client_hits[0] > 60:
        _client_hits.popleft()
    if len(_client_hits) >= _CLIENT_PER_MIN:
        return {"ok": False, "reason": "rate_limited"}
    _client_hits.append(now)
    log_buffer.add_entry(
        body.level, "client", "browser", body.message,
        location=body.location[:300], exc=body.stack, request=body.page[:200],
    )
    return {"ok": True}


@router.delete("/api/logs")
async def clear_logs():
    return {"ok": True, "deleted": log_buffer.clear()}
