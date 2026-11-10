"""
앱 전체 로그 수집기 (로그 탭용)

- 파이썬 logging 의 모든 로그를 메모리 링버퍼에 모은다 (서버/크롤러/라이브러리).
- WARNING 이상은 data/logs/events.jsonl 에도 저장해 서버가 재시작돼도 남긴다.
- 처리되지 않은 예외와 HTTP 4xx/5xx 응답은 '어떤 요청에서' 났는지(요청 경로)와 함께 기록한다.
- 브라우저(JS) 오류는 /api/logs/client 로 받아 같은 버퍼에 넣는다.
"""

import contextvars
import json
import logging
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "data" / "logs"
_LOG_FILE = _LOG_DIR / "events.jsonl"

MAX_ENTRIES = 3000                 # 메모리에 유지할 최대 로그 수
_PERSIST_LEVEL = logging.WARNING   # 이 레벨 이상만 파일에 저장
_FILE_MAX_BYTES = 5 * 1024 * 1024  # 파일이 이보다 커지면 시작 시 최근 2000줄만 남김
_MSG_MAX = 4000
_EXC_MAX = 12000

# INFO 가 쏟아져 화면을 덮는 라이브러리 (경고 이상만 기록)
_NOISY_PREFIXES = ("httpx", "httpcore", "uvicorn.access", "watchfiles", "multipart", "playwright", "hpack")

BOOT_ID = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")   # 서버가 다시 떴는지 화면이 알아채는 용도

_lock = threading.Lock()
_buf: "deque[dict]" = deque(maxlen=MAX_ENTRIES)
_next_id = 1
_installed = False

# 현재 처리 중인 HTTP 요청 ("POST /api/search"). 그 요청 중(백그라운드 태스크 포함)에 남은 로그에 붙는다.
_current_request: contextvars.ContextVar[str] = contextvars.ContextVar("current_request", default="")

_LEVEL_NO = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _rel(path: str) -> str:
    try:
        return str(Path(path).resolve().relative_to(_PROJECT_ROOT))
    except Exception:
        return Path(path).name


def add_entry(level: str, source: str, logger_name: str, message: str, *,
              location: str = "", exc: str = "", request: str = "") -> dict:
    """로그 한 건을 버퍼에 넣는다. level: INFO/WARNING/ERROR, source: server/http/client."""
    global _next_id
    level = (level or "INFO").upper()
    if level == "WARN":
        level = "WARNING"
    if level == "CRITICAL":
        level = "ERROR"
    if level not in _LEVEL_NO:
        level = "INFO"
    entry = {
        "id": 0, "ts": _now_iso(), "level": level, "source": source, "logger": logger_name,
        "message": (message or "")[:_MSG_MAX], "location": location, "exc": (exc or "")[:_EXC_MAX],
        "request": request,
    }
    with _lock:
        entry["id"] = _next_id
        _next_id += 1
        _buf.append(entry)
    if _LEVEL_NO[level] >= _PERSIST_LEVEL:
        _persist(entry)
    return entry


def _persist(entry: dict) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass   # 로그 저장 실패가 앱을 멈추게 하면 안 된다


def _load_persisted() -> None:
    """서버 재시작 후에도 이전 경고/오류가 보이도록 파일에서 최근 기록을 복원한다."""
    global _next_id
    try:
        if not _LOG_FILE.exists():
            return
        lines = _LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        if _LOG_FILE.stat().st_size > _FILE_MAX_BYTES:
            lines = lines[-2000:]
            _LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        max_id = 0
        for ln in lines[-MAX_ENTRIES:]:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if isinstance(e, dict) and "id" in e:
                _buf.append(e)
                max_id = max(max_id, int(e["id"]))
        _next_id = max_id + 1
    except Exception:
        pass


class BufferHandler(logging.Handler):
    """logging → 링버퍼."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name or ""
            if name.startswith(_NOISY_PREFIXES) and record.levelno < logging.WARNING:
                return
            exc = ""
            if record.exc_info and record.exc_info[0] is not None:
                # 요청 미들웨어가 이미 기록한 예외는 uvicorn 이 한 번 더 남기므로 중복 제외
                if getattr(record.exc_info[1], "_log_buffer_logged", False):
                    return
                exc = "".join(traceback.format_exception(*record.exc_info))
            source = "http" if name == "http" else "server"
            # HTTP 로그는 기록 위치(미들웨어)가 아니라 요청 경로가 중요하므로 location 을 비운다
            location = "" if source == "http" else f"{_rel(record.pathname)}:{record.lineno} ({record.funcName})"
            add_entry(record.levelname, source, name, record.getMessage(),
                      location=location, exc=exc, request=_current_request.get(""))
        except Exception:
            pass


def install_log_capture() -> None:
    """루트 로거 + uvicorn 로거에 핸들러를 붙인다 (여러 번 호출해도 한 번만)."""
    global _installed
    if _installed:
        return
    _installed = True
    _load_persisted()
    h = BufferHandler(level=logging.INFO)
    root = logging.getLogger()
    root.addHandler(h)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # uvicorn 로거는 propagate=False 라 루트 핸들러로 오지 않는다
    for n in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(n)
        if h not in lg.handlers:
            lg.addHandler(h)
    # 이미 떠 있는 서버 모듈 import 이전에 남은 로그는 없으므로 시작 표시만 남긴다
    logging.getLogger("app").info("로그 수집 시작 (boot=%s)", BOOT_ID)


# ── 조회 / 관리 ───────────────────────────────────────────────────────────────

def get_entries(after_id: int = 0, limit: int = 500, level: str = "", source: str = "", q: str = "") -> list[dict]:
    """after_id 보다 큰 로그를 오래된 순으로. after_id=0 이면 최근 limit 건."""
    min_no = _LEVEL_NO.get((level or "").upper(), 0)
    ql = (q or "").strip().lower()
    with _lock:
        items = list(_buf)
    out = []
    for e in items:
        if e["id"] <= after_id:
            continue
        if min_no and _LEVEL_NO.get(e["level"], 20) < min_no:
            continue
        if source and e.get("source") != source:
            continue
        if ql and ql not in (e["message"] + " " + e.get("exc", "") + " " + e.get("location", "")
                             + " " + e.get("logger", "") + " " + e.get("request", "")).lower():
            continue
        out.append(e)
    limit = max(1, min(limit, MAX_ENTRIES))
    return out[-limit:] if after_id == 0 else out[:limit]


def counts() -> dict:
    with _lock:
        items = list(_buf)
    c = {"INFO": 0, "WARNING": 0, "ERROR": 0}
    for e in items:
        c[e["level"]] = c.get(e["level"], 0) + 1
    c["total"] = len(items)
    return c


def last_id() -> int:
    with _lock:
        return _next_id - 1


def clear() -> int:
    with _lock:
        n = len(_buf)
        _buf.clear()
    try:
        if _LOG_FILE.exists():
            _LOG_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass
    return n


# ── HTTP 요청 로깅 (순수 ASGI 미들웨어) ────────────────────────────────────────

_SKIP_PATH_PREFIXES = ("/api/logs", "/static")


class RequestLogMiddleware:
    """처리되지 않은 예외와 4xx/5xx 응답을 요청 경로와 함께 기록한다."""

    def __init__(self, app):
        self.app = app
        self.log = logging.getLogger("http")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope.get("method", ""), scope.get("path", "")
        qs = scope.get("query_string", b"").decode("latin-1")
        label = f"{method} {path}" + (f"?{qs[:200]}" if qs else "")
        token = _current_request.set(f"{method} {path}")
        t0 = time.perf_counter()
        status = [0]
        body = bytearray()

        async def _send(message):
            if message["type"] == "http.response.start":
                status[0] = message["status"]
            elif message["type"] == "http.response.body" and status[0] >= 400 and len(body) < 2000:
                body.extend(message.get("body", b"")[:2000])
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as e:
            if not path.startswith(_SKIP_PATH_PREFIXES):
                self.log.error(f"처리되지 않은 예외 → 500: {label}", exc_info=True)
                try:
                    e._log_buffer_logged = True   # uvicorn 의 중복 기록 방지
                except Exception:
                    pass
            raise
        else:
            if status[0] >= 400 and not path.startswith(_SKIP_PATH_PREFIXES):
                ms = (time.perf_counter() - t0) * 1000
                detail = ""
                try:
                    d = json.loads(bytes(body).decode("utf-8", errors="replace"))
                    detail = d.get("detail", d) if isinstance(d, dict) else d
                    if not isinstance(detail, str):
                        detail = json.dumps(detail, ensure_ascii=False)
                except Exception:
                    detail = bytes(body).decode("utf-8", errors="replace")
                msg = f"HTTP {status[0]} {label} ({ms:.0f}ms)" + (f" — {detail[:500]}" if detail else "")
                (self.log.error if status[0] >= 500 else self.log.warning)(msg)
        finally:
            _current_request.reset(token)
