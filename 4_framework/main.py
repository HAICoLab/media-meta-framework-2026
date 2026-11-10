"""
K-Drama 메타데이터 수집기 - FastAPI 앱 엔트리포인트

모듈 구조:
  [1_web_crawler] core/paths.py  프로젝트 루트·DB·위키 덤프 경로 (이하 core·wiki·crawlers·services 는 1_web_crawler, routers·static 은 4_framework)
  core/database.py        SQLite DB 레이어
  core/browser.py         Playwright 브라우저 싱글톤
  core/progress.py        크롤링 진행상황 추적
  wiki/parse.py           위키피디아 문서 HTML 파싱
  wiki/resolver.py        위키 문서 선택 (수동검색·자동수집 공용)
  wiki/dump_indexer.py    위키 덤프 다운로드·인덱싱
  crawlers/filters.py     포스터·공식사이트 URL 필터
  crawlers/official.py    공식 사이트 스크래핑
  crawlers/metadata.py    메타데이터 조립·단건 수집
  crawlers/mbccb.py       MBC충북 뉴스/다시보기/LIVE
  crawlers/com2usn.py     com2usN 드라마
  crawlers/youtube.py     YouTube 제목/설명/썸네일
  routers/                FastAPI 라우터
  services/               BWC 동기화, 덤프 워커, 로그 버퍼
"""

import asyncio
import logging
import os
import sys
import pathlib
import re

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

# 크롤러·DB 모듈은 ../1_web_crawler 에 있으므로 import 경로에 추가한다
_CRAWLER_DIR = pathlib.Path(__file__).resolve().parent.parent / "1_web_crawler"
if str(_CRAWLER_DIR) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_DIR))

from core.database import init_db
from core.browser import init_browser, close_browser
from core.paths import DATA_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# 앱 전체 로그를 로그 탭에서 볼 수 있도록 수집 (다른 모듈 import 전에 설치해야 시작 로그도 잡힌다)
from services.log_buffer import install_log_capture, RequestLogMiddleware
install_log_capture()

# ── broadcast_web_crawler 로드 ────────────────────────────────────────────────
_BWC_SRC = pathlib.Path(os.environ.get("BWC_SRC", "/workspace/crawling_ykcho/broadcast_web_crawler/src"))
if str(_BWC_SRC) not in sys.path:
    sys.path.insert(0, str(_BWC_SRC))

_bwc_app = None
_BWC_AVAILABLE = False
_BWC_TOKEN = "local-no-auth-token-broadcast"

try:
    from broadcast_web_crawler.desktop_api import create_desktop_app
    _bwc_app = create_desktop_app(
        output_root=DATA_DIR / "bwc",
        token=_BWC_TOKEN,
    )
    _BWC_AVAILABLE = True
    logging.info("broadcast_web_crawler 로드 완료")
except Exception as _e:
    logging.warning(f"broadcast_web_crawler 로드 실패: {_e}")


# ── 앱 수명 관리 ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await init_browser()

    # tvN 전체 프로그램 목록(전체 조회 ~40초)을 미리 캐시해 두면 첫 검색이 느리지 않다. 실패해도 서비스에는 영향 없음.
    async def _warm_tvn_inventory():
        try:
            from services.cjenm_pair import tvn_inventory
            await tvn_inventory()
        except Exception as _e:
            logging.warning(f"tvN 목록 미리 읽기 실패(검색 시 다시 시도): {_e}")
    asyncio.create_task(_warm_tvn_inventory())
    yield

    # 종료 시 브라우저를 '먼저' 닫는다.
    # (아래에서 모든 태스크를 취소하면 Playwright 내부 통신 태스크도 같이 죽어 close() 가 영원히 응답을 기다리게 된다)
    try:
        await asyncio.wait_for(close_browser(), timeout=8)
    except (asyncio.TimeoutError, Exception):
        logging.warning("브라우저 종료가 지연되어 건너뜁니다.")

    # 종료 시 백그라운드 태스크 취소
    def _is_server_task(t) -> bool:
        # uvicorn 자신의 메인 태스크(Server.serve)를 취소하면 종료 때마다 CancelledError 트레이스백이 남는다
        try:
            return "uvicorn" in (t.get_coro().cr_code.co_filename or "")
        except Exception:
            return False

    _tasks = [t for t in asyncio.all_tasks()
              if t is not asyncio.current_task() and not _is_server_task(t)]
    for t in _tasks:
        t.cancel()
    if _tasks:
        try:
            await asyncio.gather(*_tasks, return_exceptions=True)
        except asyncio.CancelledError:
            # uvicorn 내부 태스크까지 취소되면서 이 코루틴에도 취소가 전파된다 (정상 종료 경로이므로 무시)
            pass


# ── 앱 생성 + 라우터 등록 ─────────────────────────────────────────────────────

app = FastAPI(title="K-Drama Crawler", lifespan=lifespan)

# 정적 파일
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# 라우터 등록
from routers.library  import router as library_router
from routers.crawlers import router as crawlers_router
from routers.dumps    import router as dumps_router
from routers.logs     import router as logs_router

app.include_router(library_router)
app.include_router(crawlers_router)
app.include_router(dumps_router)
app.include_router(logs_router)

# 처리되지 않은 예외 / 4xx·5xx 응답을 요청 경로와 함께 로그에 남긴다
app.add_middleware(RequestLogMiddleware)

# ── BWC 프록시 (/bwc/) ────────────────────────────────────────────────────────

if _BWC_AVAILABLE and _bwc_app is not None:
    @app.api_route("/bwc/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def bwc_proxy(path: str, request: Request):
        """broadcast_web_crawler 엔드포인트를 /bwc/ 접두사로 프록시합니다."""
        body    = await request.body()
        # tvN 수집: 이미 라이브러리에 있는 작품(pgmId)은 건너뛰고 다음 작품으로 채운다
        if request.method == "POST" and path.strip("/") == "jobs":
            try:
                import json as _json
                import aiosqlite
                from core.paths import DB_PATH
                req = _json.loads(body or b"{}")
                if "tvn.cjenm.com" in str(req.get("url", "")) and "exclude_ids" not in req:
                    async with aiosqlite.connect(DB_PATH) as _db:
                        cur = await _db.execute(
                            "SELECT source_key FROM drama_sources WHERE source='tvn' AND source_key IS NOT NULL")
                        req["exclude_ids"] = [r[0] for r in await cur.fetchall()]
                    # 별도 홍보 사이트로 리디렉션되는 작품(100일의 거짓말 등)은 수집이 항상 실패하므로 다음 수집부터 건너뛴다
                    for rep in (DATA_DIR / "bwc" / "tvn").glob("*/collection-report.json"):
                        try:
                            for rec in _json.loads(rep.read_text(encoding="utf-8")).get("programs", []):
                                if rec.get("status") == "error" and "redirect origin" in str(rec.get("error", "")) \
                                        and rec.get("program_id"):
                                    req["exclude_ids"].append(str(rec["program_id"]))
                        except Exception:
                            pass
                    body = _json.dumps(req, ensure_ascii=False).encode()
                    logging.info("tvN 수집: 기존 작품 %d건 제외", len(req["exclude_ids"]))
                # TVING 카테고리 목록 수집도 같은 방식: 이미 라이브러리에 있는 작품(P코드)은 건너뛰고 다음 작품으로 채운다
                elif re.search(r"tving\.com/more/genre/", str(req.get("url", ""))) and "exclude_ids" not in req:
                    async with aiosqlite.connect(DB_PATH) as _db:
                        cur = await _db.execute(
                            "SELECT source_key FROM drama_sources WHERE source='tving' AND source_key IS NOT NULL")
                        req["exclude_ids"] = [r[0] for r in await cur.fetchall()]
                    body = _json.dumps(req, ensure_ascii=False).encode()
                    logging.info("TVING 수집: 기존 작품 %d건 제외", len(req["exclude_ids"]))
            except Exception as _e:
                logging.warning("tvN 제외 목록 주입 실패: %s", _e)
        headers = dict(request.headers)
        headers["authorization"] = f"Bearer {_BWC_TOKEN}"
        headers["content-length"] = str(len(body))

        scope = {
            "type":         "http",
            "method":       request.method,
            "path":         f"/{path}",
            "query_string": request.url.query.encode(),
            "headers":      [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
        result_body    = []
        result_status  = [200]
        result_headers = [{}]

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                result_status[0]  = message["status"]
                result_headers[0] = dict(message.get("headers", []))
            elif message["type"] == "http.response.body":
                result_body.append(message.get("body", b""))

        await _bwc_app(scope, receive, send)
        content = b"".join(result_body)
        resp_headers = {
            k.decode(): v.decode()
            for k, v in result_headers[0].items()
            if k.decode().lower() not in {"content-length", "transfer-encoding"}
        }
        return Response(content=content, status_code=result_status[0], headers=resp_headers)

    logging.info("broadcast_web_crawler /bwc/ 라우터 등록 완료")


# ── 프론트엔드 ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(path, encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content=content, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma":        "no-cache",
        "Expires":       "0",
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
