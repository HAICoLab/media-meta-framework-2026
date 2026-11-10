"""
Wikimedia Dumps 다운로드 및 SQLite 인덱싱 스크립트

CLI 사용법:
    python -m wiki.dump_indexer --update [--force]
    python -m wiki.dump_indexer --download
    python -m wiki.dump_indexer --index
    python -m wiki.dump_indexer --status

--update 동작 (웹 UI '위키 Dumps 업데이트' 버튼이 이 모드로 실행됨):
    1. 위키 서버에 HEAD 요청 → 최신 덤프의 Last-Modified / 크기 확인
    2. 저장된 인덱스와 같으면 "이미 최신 버전" 으로 종료
    3. 덤프 다운로드 (.part 로 받고 이어받기 지원, 완료 시 교체)
    4. 임시 DB(.new)에 드라마/예능 문서만 필터링해서 인덱싱
    5. 검증 후 기존 인덱스 DB를 원자적으로 교체 (실패/취소 시 기존 인덱스 유지)
    진행상황은 data/dump_update_state.json 에 기록되어 서버가 읽어갑니다.
"""

import argparse
import bz2
import fcntl
import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree.ElementTree import iterparse

from core.paths import ROOT, WIKI_DUMP as DUMP_FILE, WIKI_INDEX as INDEX_DB, DATA_DIR

BASE_DIR = ROOT  # services.dump_updater 워커 cwd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DUMP_URL = "https://dumps.wikimedia.org/kowiki/latest/kowiki-latest-pages-articles.xml.bz2"

# 업데이트 부속 파일
PART_FILE      = DUMP_FILE.with_name(DUMP_FILE.name + ".part")        # 다운로드 중인 파일
PART_INFO_FILE = DUMP_FILE.with_name(DUMP_FILE.name + ".part.info")   # .part 가 어떤 버전인지
DUMP_INFO_FILE = DUMP_FILE.with_name(DUMP_FILE.name + ".info.json")   # 완성된 덤프 버전 정보
NEW_INDEX_DB   = INDEX_DB.with_name(INDEX_DB.name + ".new")           # 새로 만드는 중인 인덱스
STATE_FILE     = DATA_DIR / "dump_update_state.json"
LOCK_FILE      = DATA_DIR / "dump_update.lock"

_HTTP_HEADERS = {
    "User-Agent": (
        "KDramaResearch/1.0 (Academic Research; admin@example.com) "
        "Python-urllib/3.10"
    )
}

# 드라마/TV 프로그램 관련 분류 키워드
DRAMA_CAT_KEYWORDS = [
    "텔레비전 드라마", "드라마", "TV 드라마", "텔레비전 시리즈",
    "웹드라마", "OTT 드라마", "넷플릭스", "한국 드라마",
]

# 네임스페이스 0 = 일반 문서
NS_ARTICLE = "0"

# ── Wikitext 파서 ─────────────────────────────────────────────────────────────

def parse_infobox(wikitext: str) -> dict:
    """Wikitext에서 드라마 인포박스를 파싱하여 dict 반환"""
    try:
        import mwparserfromhell as mwp
        wcode = mwp.parse(wikitext)
        for template in wcode.filter_templates():
            name = str(template.name).strip()
            # 드라마 관련 템플릿
            if any(kw in name for kw in ["드라마", "텔레비전", "방송", "TV", "미니시리즈"]):
                result = {}
                for param in template.params:
                    key = str(param.name).strip()
                    val = str(param.value).strip()
                    # ── 위키 마크업 정리 ────────────────────────────────────
                    # 1) 파일/이미지 링크: [[파일:xxx|40px|link=SBS TV]] → "SBS TV"
                    #    link= 없으면 완전히 제거
                    def _repl_file(m):
                        s = m.group(0)
                        lm = re.search(r"\|link=([^\]|]+)", s)
                        return lm.group(1).strip() if lm else ""
                    val = re.sub(
                        r"\[\[(?:파일|File|이미지|Image):[^\]]*?\]\]",
                        _repl_file, val, flags=re.IGNORECASE
                    )
                    # 2) 일반 위키링크: [[링크|텍스트]] → 텍스트, [[링크]] → 링크
                    val = re.sub(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]", r"\1", val)
                    val = re.sub(r"\{\{[^}]+\}\}", "", val)
                    val = re.sub(r"<[^>]+>", "", val)
                    val = re.sub(r"<!--.*?-->", "", val, flags=re.S)
                    val = re.sub(r"\s+", " ", val).strip()
                    if key and val:
                        result[key] = val
                if result:
                    return result
        return {}
    except Exception as e:
        logging.debug(f"인포박스 파싱 실패: {e}")
        return {}


def parse_categories(wikitext: str) -> list[str]:
    """Wikitext에서 분류 목록 추출"""
    cats = re.findall(r"\[\[분류:([^\]]+)\]\]", wikitext)
    return [c.split("|")[0].strip() for c in cats]


def parse_external_links(wikitext: str) -> list[dict]:
    """Wikitext에서 외부링크 섹션 파싱"""
    # == 외부 링크 == 섹션 찾기
    m = re.search(r"==\s*외부\s*링크\s*==(.+?)(?:==|\Z)", wikitext, re.S)
    if not m:
        return []
    section = m.group(1)
    links = []
    for m2 in re.finditer(r"\[(https?://[^\s\]]+)\s*([^\]]*)\]", section):
        url, text = m2.group(1).strip(), m2.group(2).strip()
        if url:
            links.append({"url": url, "text": text or url})
    return links


def parse_summary(wikitext: str) -> str:
    """첫 단락 요약 추출"""
    try:
        import mwparserfromhell as mwp
        wcode = mwp.parse(wikitext)
        text = wcode.strip_code()
        # 첫 비어있지 않은 단락
        for para in text.split("\n"):
            para = para.strip()
            if para and not para.startswith("=") and not para.startswith("{") and len(para) > 30:
                return para[:500]
        return ""
    except Exception:
        return ""


def is_drama_page(wikitext: str, categories: list[str]) -> bool:
    """드라마 페이지 여부 판별"""
    # 분류 확인
    for cat in categories:
        if any(kw in cat for kw in DRAMA_CAT_KEYWORDS):
            return True
    # 인포박스 확인
    # '{{방송인 정보}}' 같은 인물 인포박스는 제외 (방송(?!인))
    if re.search(r"\{\{(?:드라마|텔레비전|방송(?!인)|TV|미니시리즈)", wikitext):
        return True
    return False


# ── DB 초기화 ─────────────────────────────────────────────────────────────────

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS wiki_pages (
            title            TEXT PRIMARY KEY,
            infobox_json     TEXT DEFAULT '{}',
            external_links_json TEXT DEFAULT '[]',
            categories_json  TEXT DEFAULT '[]',
            wiki_poster_url  TEXT DEFAULT '',
            summary          TEXT DEFAULT '',
            indexed_at       TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_title ON wiki_pages(title);
    """)
    conn.commit()


class UpdateCancelled(Exception):
    """사용자가 업데이트를 취소함 (SIGTERM)"""


# ── 원격/로컬 버전 정보 ───────────────────────────────────────────────────────

def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_now_str() -> str:
    return _utc_str(datetime.now(timezone.utc))


def fetch_remote_info(timeout: int = 30) -> dict:
    """위키 서버에 HEAD 요청 → 최신 덤프의 {last_modified(UTC 문자열), size(bytes)}"""
    req = urllib.request.Request(DUMP_URL, headers=_HTTP_HEADERS, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        lm = resp.headers.get("Last-Modified") or ""
        size = int(resp.headers.get("Content-Length") or 0)
    try:
        lm_str = _utc_str(parsedate_to_datetime(lm)) if lm else ""
    except Exception:
        lm_str = ""
    return {"last_modified": lm_str, "size": size}


def read_index_meta(path: Path = INDEX_DB) -> dict:
    """인덱스 DB의 meta 테이블을 dict 로 반환 (없으면 {})"""
    if not path.exists():
        return {}
    try:
        con = sqlite3.connect(str(path))
        try:
            return dict(con.execute("SELECT key, value FROM meta").fetchall())
        finally:
            con.close()
    except Exception:
        return {}


def compare_versions(remote: dict, meta: dict | None = None) -> dict:
    """원격 최신 덤프({last_modified, size})와 현재 인덱스를 비교한다.
    업데이트 워커(run_update)와 화면의 '최신 여부 확인'이 같은 기준을 쓰도록 한 곳에 둔다.
    반환: {up_to_date, index_ok, stored_last_modified, remote_last_modified, remote_size}"""
    meta = read_index_meta() if meta is None else meta
    remote_lm = remote.get("last_modified") or ""
    remote_size = remote.get("size") or 0
    index_ok = INDEX_DB.exists() and int(meta.get("drama_count") or 0) > 0
    stored_lm = meta.get("dump_last_modified") or ""

    up_to_date = False
    if index_ok:
        if stored_lm and remote_lm:
            up_to_date = stored_lm == remote_lm
        else:  # 이전 버전 인덱스 (기준일 미기록) → 덤프 파일 크기로 비교
            up_to_date = DUMP_FILE.exists() and DUMP_FILE.stat().st_size == remote_size
    return {
        "up_to_date": up_to_date, "index_ok": index_ok,
        "stored_last_modified": stored_lm or None,
        "remote_last_modified": remote_lm or None, "remote_size": remote_size,
    }


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── 다운로드 ──────────────────────────────────────────────────────────────────

def download_dump(progress_cb=None, remote: dict | None = None):
    """Wikimedia Dumps 파일 다운로드 (.part 로 받고 이어받기 지원, 완료 시 DUMP_FILE 로 교체)

    progress_cb(downloaded_bytes, total_bytes, bytes_per_sec)
    """
    remote = remote or fetch_remote_info()
    total = remote["size"]
    remote_lm = remote["last_modified"]

    # 같은 버전의 .part 만 이어받기
    resume_from = 0
    if PART_FILE.exists():
        prev = _read_json(PART_INFO_FILE)
        part_size = PART_FILE.stat().st_size
        if prev.get("last_modified") == remote_lm and 0 < part_size < total:
            resume_from = part_size
            logging.info(f"이어받기: {part_size / 1024 / 1024:.1f} MB 부터")
        else:
            PART_FILE.unlink(missing_ok=True)

    PART_INFO_FILE.write_text(
        json.dumps({"last_modified": remote_lm, "size": total}), encoding="utf-8"
    )

    headers = dict(_HTTP_HEADERS)
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"

    logging.info(f"다운로드 시작: {DUMP_URL} → {PART_FILE}")
    req = urllib.request.Request(DUMP_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resume_from and getattr(resp, "status", 200) != 206:
            resume_from = 0  # 서버가 Range 를 무시 → 처음부터
        downloaded = resume_from
        t0 = time.time()
        last_report = 0.0
        with open(PART_FILE, "ab" if resume_from else "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if progress_cb and now - last_report >= 1.0:
                    speed = (downloaded - resume_from) / max(now - t0, 1e-6)
                    progress_cb(downloaded, total, speed)
                    last_report = now

    size = PART_FILE.stat().st_size
    if total and size != total:
        raise RuntimeError(
            f"다운로드가 중간에 끊겼습니다 ({size / 1024 / 1024:.0f} / {total / 1024 / 1024:.0f} MB). "
            "다시 업데이트하면 이어받습니다."
        )
    os.replace(PART_FILE, DUMP_FILE)
    PART_INFO_FILE.unlink(missing_ok=True)
    DUMP_INFO_FILE.write_text(
        json.dumps({"last_modified": remote_lm, "size": size}), encoding="utf-8"
    )
    if progress_cb:
        progress_cb(size, total or size, 0)
    logging.info(f"다운로드 완료: {DUMP_FILE} ({size / 1024 / 1024:.1f} MB)")


# ── 인덱싱 ────────────────────────────────────────────────────────────────────

def index_dump(limit: int = 0, dump_file: Path = DUMP_FILE, index_db: Path = INDEX_DB,
               progress_cb=None, extra_meta: dict | None = None) -> tuple[int, int]:
    """Dumps XML을 파싱하여 SQLite에 인덱싱 (드라마/TV 관련 페이지만 저장)

    progress_cb(read_bytes, total_bytes, pages_scanned, drama_pages)
    반환: (전체 문서 수, 드라마 문서 수)
    """
    if not dump_file.exists():
        raise FileNotFoundError(f"Dumps 파일 없음: {dump_file} (먼저 다운로드하세요)")

    conn = sqlite3.connect(str(index_db))
    init_db(conn)

    count_total = 0
    count_drama = 0
    t0 = time.time()
    last_report = 0.0
    file_size = dump_file.stat().st_size

    logging.info("인덱싱 시작... (드라마/TV 관련 페이지만 저장됩니다)")

    raw = open(dump_file, "rb")
    try:
        with bz2.BZ2File(raw) as f:
            # start 이벤트로 root 를 잡아두고 페이지마다 비워서 메모리 증가를 막음
            context = iterparse(f, events=("start", "end"))
            root = None
            cur_page: dict = {}

            for event, elem in context:
                if event == "start":
                    if root is None:
                        root = elem
                    continue

                tag = elem.tag.rsplit("}", 1)[-1]  # 네임스페이스 버전에 무관하게 처리

                if tag == "ns":
                    cur_page["ns"] = elem.text or ""
                elif tag == "title":
                    cur_page["title"] = elem.text or ""
                elif tag == "text":
                    cur_page["wikitext"] = elem.text or ""
                elif tag == "page":
                    ns = cur_page.get("ns", "")
                    title = cur_page.get("title", "")
                    wikitext = cur_page.get("wikitext", "")

                    if ns == NS_ARTICLE and title and wikitext:
                        count_total += 1
                        cats = parse_categories(wikitext)

                        if is_drama_page(wikitext, cats):
                            infobox = parse_infobox(wikitext)
                            ext_links = parse_external_links(wikitext)
                            summary = parse_summary(wikitext)
                            count_drama += 1

                            conn.execute(
                                """INSERT OR REPLACE INTO wiki_pages
                                   (title, infobox_json, external_links_json, categories_json, summary, indexed_at)
                                   VALUES (?, ?, ?, ?, ?, datetime('now'))""",
                                (title, json.dumps(infobox, ensure_ascii=False),
                                 json.dumps(ext_links, ensure_ascii=False),
                                 json.dumps(cats, ensure_ascii=False),
                                 summary)
                            )
                            if count_drama % 200 == 0:
                                conn.commit()

                        if count_total % 500 == 0:
                            now = time.time()
                            if now - last_report >= 1.0:
                                last_report = now
                                if progress_cb:
                                    progress_cb(raw.tell(), file_size, count_total, count_drama)
                                if count_total % 20000 == 0:
                                    logging.info(
                                        f"  진행: 전체 {count_total}개 중 드라마 {count_drama}개 저장 "
                                        f"({now - t0:.0f}초)"
                                    )

                        if limit and count_total >= limit:
                            break

                    cur_page = {}
                    elem.clear()
                    if root is not None:
                        root.clear()
    finally:
        raw.close()

    conn.commit()

    # 메타 정보 저장
    meta = {
        "indexed_at": utc_now_str(),
        "drama_count": str(count_drama),
        "total_pages": str(count_total),
    }
    meta.update({k: str(v) for k, v in (extra_meta or {}).items()})
    conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", list(meta.items()))
    conn.commit()
    conn.close()

    if progress_cb:
        progress_cb(file_size, file_size, count_total, count_drama)
    logging.info(
        f"인덱싱 완료: 전체 {count_total}개 → 드라마 {count_drama}개 저장 ({time.time() - t0:.1f}초)"
    )
    return count_total, count_drama


# ── 상태 확인 ─────────────────────────────────────────────────────────────────

def show_status():
    """인덱스 상태 출력"""
    dump_exists = DUMP_FILE.exists()
    index_exists = INDEX_DB.exists()

    print(f"Dumps 파일: {'✓ ' + str(DUMP_FILE.stat().st_size // (1024*1024)) + 'MB' if dump_exists else '✗ 없음'}")
    print(f"인덱스 DB:  {'✓' if index_exists else '✗ 없음'}")

    if index_exists:
        conn = sqlite3.connect(str(INDEX_DB))
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        count = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
        conn.close()
        print(f"  - 드라마 페이지 수: {count}개")
        print(f"  - 마지막 인덱싱:    {meta.get('indexed_at', '알 수 없음')}")
        print(f"  - 덤프 기준일:      {meta.get('dump_last_modified', '알 수 없음')}")


# ── 전체 업데이트 (확인 → 다운로드 → 인덱싱 → 교체) ───────────────────────────

# 전체 진행률(%) 구간
_PCT_CHECK_END = 2
_PCT_DOWNLOAD_END = 30   # 다운로드가 필요 없으면 이 구간은 건너뜀


class _State:
    """data/dump_update_state.json 을 원자적으로 갱신 (서버가 폴링해서 UI에 표시)"""

    def __init__(self, force: bool):
        prev = _read_json(STATE_FILE)
        self.d = {
            "status": "checking",
            "message": "위키피디아 서버에서 최신 덤프 정보를 확인하는 중...",
            "percent": 0,
            "detail": "",
            "pid": os.getpid(),
            "force": force,
            "started_at": utc_now_str(),
            "finished_at": None,
            "error": None,
            "remote_last_modified": None,
            "remote_size": None,
            "eta_seconds": None,
            # 이전 실행에서 이어받는 값
            "last_checked_at": prev.get("last_checked_at"),
        }
        self.save()

    def set(self, **kw):
        self.d.update(kw)
        self.save()

    def save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATE_FILE)


def _fmt_mb(n: float) -> str:
    return f"{n / 1024 / 1024:,.0f} MB"


def _fmt_eta(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec // 60}분 {sec % 60}초"
    return f"{sec // 3600}시간 {(sec % 3600) // 60}분"


def run_update(force: bool = False) -> int:
    """최신 여부 확인 → (필요 시) 다운로드 → 인덱싱 → 교체. 종료코드 반환(0=정상/최신, 1=오류, 2=취소)"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 중복 실행 방지
    lock = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logging.error("이미 다른 업데이트가 실행 중입니다.")
        return 1

    def _on_term(signum, frame):
        raise UpdateCancelled()

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    st = _State(force)
    try:
        # ① 최신 여부 확인 ───────────────────────────────────────────
        remote = fetch_remote_info()
        remote_lm, remote_size = remote["last_modified"], remote["size"]
        st.set(remote_last_modified=remote_lm or None, remote_size=remote_size,
               last_checked_at=utc_now_str(), percent=_PCT_CHECK_END)

        meta = read_index_meta()
        cmp = compare_versions(remote, meta)
        stored_lm = cmp["stored_last_modified"] or ""
        up_to_date = cmp["up_to_date"]

        if up_to_date and not force:
            if not stored_lm and remote_lm:  # 다음부터 날짜로 비교할 수 있게 기록
                try:
                    con = sqlite3.connect(str(INDEX_DB))
                    con.executemany(
                        "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                        [("dump_last_modified", remote_lm), ("dump_size", str(remote_size))],
                    )
                    con.commit()
                    con.close()
                except Exception as e:
                    logging.warning(f"meta 기록 실패: {e}")
            st.set(
                status="up_to_date", percent=100, finished_at=utc_now_str(),
                message="이미 최신 버전입니다. 업데이트할 내용이 없습니다.",
                detail=f"덤프 기준일 {remote_lm or '알 수 없음'} · 드라마/예능 문서 {int(meta.get('drama_count') or 0):,}개",
            )
            return 0

        # ② 다운로드 (이미 같은 버전 파일이 있으면 건너뜀) ───────────────
        have_dump = DUMP_FILE.exists() and DUMP_FILE.stat().st_size == remote_size
        if have_dump:
            info = _read_json(DUMP_INFO_FILE)
            if info.get("last_modified") and remote_lm and info["last_modified"] != remote_lm:
                have_dump = False
        if have_dump and not DUMP_INFO_FILE.exists():
            DUMP_INFO_FILE.write_text(
                json.dumps({"last_modified": remote_lm, "size": remote_size}), encoding="utf-8"
            )

        if have_dump:
            idx_start, idx_span = _PCT_CHECK_END, 99 - _PCT_CHECK_END
        else:
            idx_start, idx_span = _PCT_DOWNLOAD_END, 99 - _PCT_DOWNLOAD_END

            def _dl(done, total, speed):
                frac = done / total if total else 0
                eta = (total - done) / speed if speed > 0 else None
                st.set(
                    status="downloading",
                    message=f"덤프 다운로드 중... {frac * 100:.1f}%",
                    detail=f"{_fmt_mb(done)} / {_fmt_mb(total)}"
                           + (f" · {speed / 1024 / 1024:.1f} MB/s" if speed else "")
                           + (f" · 남은 시간 약 {_fmt_eta(eta)}" if eta else ""),
                    percent=round(_PCT_CHECK_END + (_PCT_DOWNLOAD_END - _PCT_CHECK_END) * frac, 1),
                    eta_seconds=int(eta) if eta else None,
                )

            st.set(status="downloading", message="덤프 다운로드를 시작합니다...", detail="")
            download_dump(progress_cb=_dl, remote=remote)

        # ③ 인덱싱 (임시 DB 에 생성) ─────────────────────────────────
        NEW_INDEX_DB.unlink(missing_ok=True)
        t_index = time.time()

        def _idx(read_b, total_b, scanned, dramas):
            frac = read_b / total_b if total_b else 0
            elapsed = time.time() - t_index
            eta = elapsed / frac * (1 - frac) if frac > 0.01 else None
            st.set(
                status="indexing",
                message=f"드라마/예능 문서 필터링 중... {frac * 100:.1f}%",
                detail=f"문서 {scanned:,}개 검사 · 드라마/예능 {dramas:,}개 선별"
                       + (f" · 남은 시간 약 {_fmt_eta(eta)}" if eta else ""),
                percent=round(idx_start + idx_span * frac, 1),
                eta_seconds=int(eta) if eta else None,
            )

        st.set(status="indexing", message="드라마/예능 문서 필터링을 시작합니다...", detail="",
               percent=idx_start)
        total_pages, drama_count = index_dump(
            dump_file=DUMP_FILE, index_db=NEW_INDEX_DB, progress_cb=_idx,
            extra_meta={"dump_last_modified": remote_lm, "dump_size": remote_size,
                        "source_url": DUMP_URL},
        )

        # ④ 검증 후 교체 ─────────────────────────────────────────────
        st.set(status="finalizing", message="새 인덱스를 검증하고 교체하는 중...",
               detail="", percent=99.5, eta_seconds=None)
        old_count = int(meta.get("drama_count") or 0)
        if drama_count < 1000:
            raise RuntimeError(
                f"인덱싱 결과가 비정상입니다 (드라마/예능 {drama_count}개). 기존 인덱스를 유지합니다."
            )
        if old_count and drama_count < old_count * 0.5:
            raise RuntimeError(
                f"새 인덱스({drama_count:,}개)가 기존({old_count:,}개)의 절반 미만입니다. "
                "기존 인덱스를 유지합니다."
            )
        os.replace(NEW_INDEX_DB, INDEX_DB)

        st.set(
            status="done", percent=100, finished_at=utc_now_str(),
            message="업데이트가 완료되었습니다.",
            detail=f"드라마/예능 문서 {drama_count:,}개 (기존 {old_count:,}개) · 덤프 기준일 {remote_lm or '알 수 없음'}",
            eta_seconds=None,
        )
        return 0

    except UpdateCancelled:
        NEW_INDEX_DB.unlink(missing_ok=True)
        st.set(status="cancelled", finished_at=utc_now_str(), eta_seconds=None,
               message="업데이트를 취소했습니다.",
               detail="기존 인덱스는 그대로 유지됩니다. (다운로드 중이었다면 다음에 이어받습니다)")
        return 2
    except Exception as e:
        logging.error("덤프 업데이트 실패\n" + traceback.format_exc())
        NEW_INDEX_DB.unlink(missing_ok=True)
        st.set(status="error", finished_at=utc_now_str(), eta_seconds=None,
               message="업데이트에 실패했습니다.", error=str(e),
               detail="기존 인덱스는 그대로 유지됩니다.")
        return 1
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        except Exception:
            pass


# ── 엔트리포인트 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Wikimedia Dumps 인덱서")
    parser.add_argument("--update",   action="store_true", help="최신 확인 → 다운로드 → 인덱싱 → 교체 (전체 자동)")
    parser.add_argument("--force",    action="store_true", help="--update 시 최신이어도 다시 인덱싱")
    parser.add_argument("--download", action="store_true", help="Dumps 파일 다운로드")
    parser.add_argument("--index",    action="store_true", help="SQLite 인덱싱")
    parser.add_argument("--status",   action="store_true", help="인덱스 상태 확인")
    parser.add_argument("--limit",    type=int, default=0, help="테스트용 페이지 수 제한")
    args = parser.parse_args()

    if args.update:
        sys.exit(run_update(force=args.force))

    if args.status or not any([args.download, args.index]):
        show_status()
    if args.download:
        download_dump()
    if args.index:
        index_dump(limit=args.limit)
