"""TVING 번들(data/bwc/tving/**/metadata.json) → 출처별 원본 레코드.

parse_tving_bundle(meta) -> dict   (파일/DB 접근 없는 순수 함수. tvN 의 parse_tvn_bundle 과 같은 모양)
  source='tving', source_key(P코드), url, title, channel, kind(예능/드라마/교양), genre, status,
  poster, synopsis, people[]  (TVING 은 출연자 실명만 준다 → kind='cast'), episodes[]
episodes[] 는 tvN 레코드와 같은 키(no/title/tagline/synopsis/posted_at)를 쓴다.
"""
from __future__ import annotations

import re
from typing import Any

_KIND_BY_CATEGORY = (("예능", "예능"), ("드라마", "드라마"), ("교양", "교양"), ("다큐", "교양"))


def _date(raw: Any) -> str:
    t = str(raw or "")
    return f"{t[:4]}-{t[4:6]}-{t[6:8]}" if re.fullmatch(r"\d{8}", t) else ""


def _kind(category: str, genre: str) -> str:
    for needle, kind in _KIND_BY_CATEGORY:
        if needle in (category or ""):
            return kind
    return "예능" if "예능" in (genre or "") else ""


def _best_poster(ci: dict) -> str:
    from services.bwc_sync import _best_image
    return _best_image(ci.get("image") or [])


def _episodes(meta: dict) -> list[dict]:
    rows = (((meta.get("episodeApi") or {}).get("body") or {}).get("result")) or []
    out = []
    for row in rows:
        ep = (row or {}).get("episode") or {}
        if not ep.get("code"):
            continue
        no = ep.get("frequency")
        title = ((row.get("vod_name") or {}).get("ko") or "").strip() or (f"{no}화" if no else ep["code"])
        out.append({
            "no": int(no) if isinstance(no, (int, float)) or str(no or "").isdigit() else None,
            "title": title, "tagline": "",
            "synopsis": ((ep.get("synopsis") or {}).get("ko") or "").strip(),
            "posted_at": _date(ep.get("broadcast_date")),
            "code": ep["code"],
        })
    out.sort(key=lambda e: (e["no"] is None, e["no"] or 0))
    return out


def parse_tving_bundle(meta: dict) -> dict | None:
    ci = meta.get("contentInfo") or {}
    code = ci.get("program_code") or ""
    title = (ci.get("title") or "").strip()
    if not code or not title:
        return None
    category = ci.get("category_name") or ""
    genre = ci.get("genre_name") or ""
    cast = [n.strip() for n in (ci.get("actor") or []) if isinstance(n, str) and n.strip()]
    start = _date(ci.get("broad_dt"))
    end = _date(ci.get("broad_end_dt"))
    return {
        "source": "tving",
        "source_key": code,
        "url": f"https://www.tving.com/contents/{code}",
        "title": title,
        "channel": (ci.get("channel") or "").strip(),
        "kind": _kind(category, genre),
        "category": category,
        "genre": genre,
        "status": ci.get("broad_state") or "",
        "start_date": start,
        "end_date": end,
        "year": int(start[:4]) if start else (int(ci["product_year"]) if str(ci.get("product_year") or "").isdigit() else None),
        "poster": _best_poster(ci),
        "synopsis": (ci.get("synopsis") or "").strip(),
        "directors": [d for d in (ci.get("director") or []) if d],
        "writers": [w for w in (ci.get("script") or []) if w],
        "people": [{"kind": "cast", "group": "출연", "name": n, "brief": "", "detail": "", "image": "", "order": i}
                   for i, n in enumerate(cast)],
        "episodes": _episodes(meta),
        "collected_at": (meta.get("collection") or {}).get("collected_at") or "",
    }
