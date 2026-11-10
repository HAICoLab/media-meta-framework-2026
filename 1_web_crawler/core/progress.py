"""
크롤링 진행상황 추적 모듈
- 전역 딕셔너리에 태스크 진행 상태 저장
- API에서 폴링해 프론트엔드에 전달
"""

import datetime

_task_progress: dict = {}


def make_progress(task_id: str, label: str):
    """진행상황 초기화."""
    _task_progress[task_id] = {
        "id":          task_id,
        "label":       label,
        "running":     True,
        "current":     0,
        "total":       0,
        "message":     "시작 중...",
        "started_at":  datetime.datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
    }


def update_progress(task_id: str, current: int, total: int, message: str):
    """진행상황 업데이트."""
    if task_id in _task_progress:
        _task_progress[task_id].update({
            "current": current,
            "total":   total,
            "message": message,
        })


def finish_progress(task_id: str, saved: int):
    """태스크 완료 처리."""
    if task_id in _task_progress:
        _task_progress[task_id].update({
            "running":     False,
            "message":     f"완료 ({saved}개 저장)",
            "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        })


def get_all_progress() -> list:
    """모든 태스크 진행상황 반환."""
    return list(_task_progress.values())
