"""tvN 공식 홈페이지 번들(data/bwc/tvn/**/metadata.json) 파서.

번들은 tvN 페이지가 내려주는 모듈(modules)·연결 페이지(linkedPages)·미리보기 API 응답을 그대로 담고 있다.
모듈 ID(27919 등)는 작품마다 달라질 수 있으므로 ID가 아니라 **데이터 키의 모양**으로 모듈을 찾는다.
(현재 번들 19건 모두 요약/인물/미리보기/클립 모듈의 키 구성이 같음을 확인함)

parse_tvn_bundle(meta) -> 출처별 원본 레코드(dict). 파일/DB 접근 없는 순수 함수.
  source, source_key(pgmId), url, title, kind(예능/드라마/교양), sub_genre, status, air_time_text, poster,
  intent_short(기획의도 요약), intro_long(프로그램 소개), tving_code/tving_url(TVING 다시보기 링크에서 추출),
  sns[], people[], episodes[], clips[], ost[]

people[].kind
  character = 드라마 등장인물(배역명) — 배우 이름은 이 모듈에 없다 (위키/TVING 으로 연결)
  cast      = 예능/교양 출연진(MC·패널 실명)
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Iterator

_KST = timezone(timedelta(hours=9))
_TVING_CODE_RE = re.compile(r"tving\.com/contents/([PE]\d+)")
_EP_NO_RE = re.compile(r"(\d+)\s*(?:회|화)")
_CAST_GROUP_RE = re.compile(r"출연|MC|패널|게스트|진행|멤버")


def clean_text(s: Any) -> str:
    """HTML 태그/엔티티·CRLF 정리. 줄바꿈은 유지하고 3줄 이상 연속 공백 줄은 줄인다."""
    if not s:
        return ""
    t = str(s)
    t = re.sub(r"<\s*br\s*/?>|</p\s*>|</div\s*>|</li\s*>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t).replace("\xa0", " ")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def _one_line(s: Any) -> str:
    return re.sub(r"\s+", " ", clean_text(s)).strip()


_IMG_HOST = "https://poc-cf-image.cjenm.com/public"   # tvN 페이지가 상대경로(/pcms/…, /share/…) 이미지에 붙이는 CDN


def abs_image(url: Any) -> str:
    """tvN 이미지 경로가 '/'로 시작하면 CDN 주소를 붙여 절대 URL로 만든다 (한글 파일명은 퍼센트 인코딩)."""
    u = str(url or "").strip()
    if not u.startswith("/") or u.startswith("//"):
        return u
    from urllib.parse import quote
    path, sep, query = u.partition("?")
    return _IMG_HOST + quote(path, safe="/%") + sep + query


def extract_tving_code(url: str) -> str:
    """TVING 링크(직접 링크 / tving.page.link 래핑)에서 P·E 코드를 뽑는다."""
    if not url:
        return ""
    m = _TVING_CODE_RE.search(url)
    if not m:
        # tving.page.link/?link=https%3A%2F%2Fwww.tving.com%2Fcontents%2FP... 형태
        from urllib.parse import unquote
        m = _TVING_CODE_RE.search(unquote(url))
    return m.group(1) if m else ""


def _iter_module_data(meta: dict) -> Iterator[tuple[str, Any]]:
    for mid, m in (meta.get("modules") or {}).items():
        yield mid, (m or {}).get("data")


def _find_dict(meta: dict, key: str) -> dict | None:
    for _, dt in _iter_module_data(meta):
        if isinstance(dt, dict) and key in dt:
            return dt
    return None


def _all_preview_items(meta: dict) -> list[dict]:
    """미리보기 항목을 모든 출처(모듈, linkedPages 목록, previewApiResponses)에서 모아 중복 제거."""
    items: list[dict] = []

    def _from_modules(mods: dict):
        for _, m in (mods or {}).items():
            dt = (m or {}).get("data")
            if isinstance(dt, dict):
                if isinstance(dt.get("previewInfoList"), list):
                    items.extend(dt["previewInfoList"])
                di = dt.get("dataInfo")
                if isinstance(di, dict) and isinstance(di.get("list"), list):
                    items.extend(di["list"])

    _from_modules(meta.get("modules"))
    for lp in meta.get("linkedPages") or []:
        _from_modules(lp.get("modules"))
    for pr in meta.get("previewApiResponses") or []:
        res = ((pr or {}).get("response") or {}).get("result") or {}
        dt = res.get("data")
        if isinstance(dt, dict):
            di = dt.get("dataInfo")
            if isinstance(di, dict) and isinstance(di.get("list"), list):
                items.extend(di["list"])

    seen, out = set(), []
    for it in items:
        if not isinstance(it, dict) or "prevewTit" not in it:
            continue
        key = it.get("pgmTmnoPrevewSeq") or it.get("arrayId") or it.get("prevewTit")
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _episodes(meta: dict) -> list[dict]:
    eps = []
    for it in _all_preview_items(meta):
        title = _one_line(it.get("prevewTit"))
        m = _EP_NO_RE.search(title)
        ts = it.get("putupStrDtm")
        posted = ""
        if isinstance(ts, (int, float)) and ts > 0:
            posted = datetime.fromtimestamp(ts / 1000, _KST).isoformat(timespec="seconds")
        eps.append({
            "no": int(m.group(1)) if m else None,
            "title": title,
            "tagline": _one_line(it.get("prevewSubSjNm")),   # 회차 한 줄 카피
            "synopsis": clean_text(it.get("prevewCnts")),
            "posted_at": posted,
        })
    # 회차 번호 내림차순 (번호 없는 항목은 뒤로)
    eps.sort(key=lambda e: (e["no"] is None, -(e["no"] or 0)))
    return eps


def _people(meta: dict, kind_hint: str) -> list[dict]:
    dt = _find_dict(meta, "detailPrsnInfoList")
    if not dt:
        return []
    people, order = [], 0
    for grp in dt.get("detailPrsnInfoList") or []:
        gname = _one_line(grp.get("prsnGropNm"))
        # 드라마는 배역, 그 외(또는 그룹명이 출연진/MC 류)는 실제 출연자
        is_cast = kind_hint != "드라마" or bool(_CAST_GROUP_RE.search(gname))
        for p in grp.get("pgmPrsnIntrcnInfoList") or []:
            name = _one_line(p.get("prsnNm"))
            if not name:
                continue
            order += 1
            brief = _one_line(p.get("adiDescpCnts"))
            if not is_cast:
                # 오래된 작품은 이름 칸에 나이가 붙어 있다: '도깨비(김신), 939세' · '임현준 (40대)' → 이름 / 설명으로 분리
                from wiki.actors import split_character_name
                base, extra = split_character_name(name)
                if base:
                    name = base
                    brief = " / ".join(x for x in (extra, brief) if x)
            people.append({
                "group": gname,
                "kind": "cast" if is_cast else "character",
                "name": name,
                "brief": brief,   # 짧은 설명: 나이/직함(드라마) · 역할(예능)
                "detail": clean_text(p.get("dtlDescpCnts")),  # 긴 설명
                "image": abs_image(p.get("prsnImgFilePathAddr")),
                "order": order,
            })
    return people


def _clips(meta: dict) -> list[dict]:
    clips = []
    for _, dt in _iter_module_data(meta):
        if isinstance(dt, list):
            for c in dt:
                if isinstance(c, dict) and "clipNm" in c:
                    clips.append({
                        "title": _one_line(c.get("clipNm")),
                        "type": _one_line(c.get("clipTypeCdNm")),
                        "episode_label": _one_line(c.get("pgmNm")),
                        "duration": c.get("playingTm") or "",
                        "thumb": c.get("videoTnailImgPathAddr") or "",
                    })
    return clips


def parse_tvn_bundle(meta: dict) -> dict:
    li = meta.get("listingItem") or {}
    col = meta.get("collection") or {}
    summary = _find_dict(meta, "plnngIntentInfo") or {}

    kind = li.get("repGenreInfo") or ""
    intent = ((summary.get("plnngIntentInfo") or {}).get("plnngIntentSumryCnts"))
    intro = ((summary.get("plnngIntentDtlPopupInfo") or {}).get("dtlCnts"))
    replay = summary.get("replayIngo") or {}
    tving_url = replay.get("pgmReplayUrlAddr") or ""

    sns = [
        {"type": s.get("snsTypeCd"), "url": s.get("snsLinkUrlAddr")}
        for s in (summary.get("snsInfoList") or []) if s.get("snsLinkUrlAddr")
    ]
    return {
        "source": "tvn",
        "source_key": li.get("pgmId") or "",
        "url": li.get("frontDetailUrlAddr") or col.get("source_url") or "",
        "title": _one_line(li.get("pgmNm")),
        "kind": kind,
        "sub_genre": li.get("ptclrGenreInfo") or "",
        "status": li.get("orgbcRendYn") or "",                  # 방송중 / 방송예정 ...
        "air_time_text": _one_line(summary.get("bdTm") or li.get("bdTm")),
        "poster": abs_image(li.get("filePathAddr")),
        "intent_short": clean_text(intent),                      # 기획의도(요약)
        "intro_long": clean_text(intro),                         # 프로그램 소개(상세)
        "tving_code": extract_tving_code(tving_url),
        "tving_url": tving_url,
        "sns": sns,
        "people": _people(meta, kind),
        "episodes": _episodes(meta),
        "clips": _clips(meta),
        "ost": [],  # 현재 번들의 ostInfoList 는 비어 있음 (구조 확인 후 채움)
        "collected_at": col.get("collected_at") or "",
    }
