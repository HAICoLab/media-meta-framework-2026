"""
위키 Dumps 업데이트 서비스

- 무거운 다운로드/인덱싱은 별도 프로세스(python -m wiki.dump_indexer --update)로 실행
  → 서버(GIL)를 막지 않고, 서버가 --reload 로 재시작돼도 업데이트는 계속 진행됨
- 진행상황은 data/dump_update_state.json 을 읽어서 제공
"""

import json
import os
import subprocess
import sys
import time

import wiki.dump_indexer as wdi

_ACTIVE_STATUSES = {"checking", "downloading", "indexing", "finalizing"}
_proc: subprocess.Popen | None = None  # 이 서버 프로세스가 띄운 워커 (좀비 수거용)


def _pid_alive(pid: int | None) -> bool:
    """pid 가 살아있는 wiki_dump_indexer 워커인지 확인 (좀비/pid 재사용 제외)"""
    if not pid:
        return False
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            stat = f.read()
        if stat.rsplit(")", 1)[1].split()[0] == "Z":  # 좀비
            return False
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"dump_indexer" in f.read()
    except Exception:
        return False


def _reap():
    global _proc
    if _proc is not None and _proc.poll() is not None:
        _proc = None


def get_update_state() -> dict:
    """업데이트 상태 (+ running 여부). 워커가 비정상 종료했으면 error 로 보정."""
    _reap()
    try:
        with open(wdi.STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {"status": "idle", "message": "", "percent": 0, "detail": ""}

    running = False
    if state.get("status") in _ACTIVE_STATUSES:
        if _pid_alive(state.get("pid")):
            running = True
        else:
            state.update(
                status="error",
                message="업데이트 프로세스가 중단되었습니다.",
                detail="서버 재부팅 등으로 중단됐을 수 있습니다. 다시 업데이트하면 이어서 진행합니다.",
                error=state.get("error") or "worker process died",
            )
    elif _proc is not None and _proc.poll() is None:
        running = True  # 방금 띄웠고 아직 상태 파일을 쓰기 전
        state = {**state, "status": "checking", "message": "시작 중...", "percent": 0}

    state["running"] = running
    return state


def get_overview() -> dict:
    """UI 에 보여줄 전체 정보: 인덱스(최종 업데이트) + 덤프 파일 + 진행상황"""
    meta = wdi.read_index_meta()
    dump_exists = wdi.DUMP_FILE.exists()
    return {
        "index": {
            "exists": wdi.INDEX_DB.exists(),
            "drama_count": int(meta.get("drama_count") or 0),
            "total_pages": int(meta.get("total_pages") or 0),
            "indexed_at": meta.get("indexed_at"),                  # UTC "YYYY-MM-DD HH:MM:SS"
            "dump_last_modified": meta.get("dump_last_modified"),  # 덤프 기준일 (UTC)
        },
        "dump_file": {
            "exists": dump_exists,
            "size_mb": round(wdi.DUMP_FILE.stat().st_size / 1024 / 1024, 1) if dump_exists else 0,
        },
        "update": get_update_state(),
    }


_REMOTE_TTL = 600  # 원격 최신 정보 캐시(초). 화면을 열 때마다 위키 서버에 요청하지 않도록
_remote_cache: dict = {"at": 0.0, "checked_at": None, "info": None}


def check_latest(refresh: bool = False) -> dict:
    """위키 덤프 배포처(dumps.wikimedia.org)의 최신 덤프와 현재 인덱스를 비교한다.
    반환: {ok, up_to_date, remote_last_modified, remote_size, stored_last_modified, index_ok, checked_at, cached}
    네트워크 실패 시 {ok: False, error} — 화면은 이 경우 버튼을 막지 않는다(판단 불가)."""
    now = time.time()
    cached = False
    if not refresh and _remote_cache["info"] and now - _remote_cache["at"] < _REMOTE_TTL:
        info, checked_at, cached = _remote_cache["info"], _remote_cache["checked_at"], True
    else:
        try:
            info = wdi.fetch_remote_info(timeout=15)
        except Exception as e:
            return {"ok": False, "error": f"위키 덤프 서버에 연결하지 못했습니다: {e}"}
        checked_at = wdi.utc_now_str()
        _remote_cache.update(at=now, checked_at=checked_at, info=info)
    if not info.get("last_modified") and not info.get("size"):
        return {"ok": False, "error": "위키 덤프 서버가 버전 정보를 주지 않았습니다."}
    return {"ok": True, "checked_at": checked_at, "cached": cached, **wdi.compare_versions(info)}


def start_update(force: bool = False) -> dict:
    """업데이트 워커 시작. 이미 실행 중이면 RuntimeError."""
    global _proc
    if get_update_state()["running"]:
        raise RuntimeError("이미 업데이트가 진행 중입니다.")

    wdi.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-u", "-m", "wiki.dump_indexer", "--update"]
    if force:
        cmd.append("--force")

    log = open(wdi.DATA_DIR / "dump_update.log", "ab")
    _proc = subprocess.Popen(
        cmd, cwd=str(wdi.BASE_DIR),
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # 서버 reload/종료와 분리
    )
    log.close()

    # 워커가 첫 상태를 기록할 때까지 잠깐 대기 (UI 가 바로 새 상태를 보도록)
    for _ in range(30):
        try:
            with open(wdi.STATE_FILE, "r", encoding="utf-8") as f:
                if json.load(f).get("pid") == _proc.pid:
                    break
        except Exception:
            pass
        if _proc.poll() is not None:
            break
        time.sleep(0.1)
    return get_update_state()


def cancel_update() -> bool:
    """진행 중인 업데이트 취소 (워커에 SIGTERM)"""
    state = get_update_state()
    if not state["running"]:
        return False
    try:
        os.kill(int(state["pid"]), 15)
    except ProcessLookupError:
        return False
    return True
