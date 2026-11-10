"""출처가 다른 레코드(tvN 공홈 ↔ TVING ↔ 기존 dramas 행)가 '같은 작품'인지 판정한다.

매칭은 3단계이며, 앞 단계에서 정해지면 멈춘다.
  1) 확정 키   : tvN 페이지가 가진 TVING 코드(P…)가 후보의 TVING 코드와 같다            → confidence 1.0
  2) 제목 규칙 : 정규화한 제목(시즌 제외)·시즌이 같고, 채널이 양립하고, 연도가 모순되지 않는다 → confidence 0.8
                (후보가 정확히 1개일 때만. 2개 이상이면 모호하므로 병합하지 않는다)
  3) 매칭 없음 : 새 작품으로 취급

2단계는 '자동 병합'이며, 같은 제목의 리메이크·타 방송사 작품을 거르기 위해 채널 충돌과 TVING 코드 충돌은 거부한다.
Candidate = {"id", "title", "channel", "year", "tving_code"}  (DB 행/TVING 번들에서 만들어 넘긴다)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# tvN 레코드와 양립하는 후보 채널 (TVING 전용 공개작은 채널이 'TVING' 이거나 비어 있다)
_TVN_COMPAT_CHANNELS = {"tvn", "tving", ""}

_SEASON_RE = re.compile(
    r"(?:\s*(?:시즌|season|s)\s*(\d+)\s*$)|(?:(?<=[^\d\s])\s*(\d{1,2})\s*기?\s*$)", re.IGNORECASE)
_NOISE_RE = re.compile(r"[\s\-_:：·・,.!?'\"“”‘’~()\[\]<>《》]+")


def split_season(title: str) -> tuple[str, int | None]:
    """'무쇠소녀단3' → ('무쇠소녀단', 3), '킹덤 시즌1' → ('킹덤', 1), '유 퀴즈 온 더 블럭' → (그대로, None).
    제목 끝 숫자가 시즌인지 제목의 일부인지는 알 수 없으므로 '앞 글자가 숫자가 아니고 1~2자리일 때만' 시즌으로 본다.
    """
    t = re.sub(r"\s*since\s*\d{4}\s*$", "", (title or "").strip(), flags=re.IGNORECASE)  # '냉장고를 부탁해 since 2014'
    m = _SEASON_RE.search(t)
    if not m:
        return t, None
    num = int(m.group(1) or m.group(2))
    base = t[:m.start()].strip()
    if not base:
        return t, None
    return base, num


def season_compatible(a: int | None, b: int | None) -> bool:
    """시즌 표기가 모순되지 않는가. 둘 다 없거나 같으면 OK, 한쪽만 있으면 그 값이 1(시즌 표기 없는 제목 = 1시즌)일 때만 OK.
    ('무쇠소녀단3' 을 TVING '무쇠소녀단'(1시즌) 에 붙이면 안 된다)"""
    if a == b:
        return True
    if a is None:
        return b == 1
    if b is None:
        return a == 1
    return False


def title_key(title: str) -> str:
    """비교용 키: 공백·구두점 제거 + 소문자. (시즌은 split_season 으로 먼저 분리해서 쓴다)"""
    return _NOISE_RE.sub("", (title or "")).lower()


def _norm_channel(ch: str) -> str:
    return (ch or "").strip().lower()


@dataclass
class MatchResult:
    method: str                      # 'tving_code' | 'title+channel+year' | 'ambiguous' | 'none' | 'rejected'
    confidence: float
    candidate: dict | None = None
    reason: str = ""
    alternatives: list[dict] = field(default_factory=list)


def _year_ok(rec_years: list[int], cand_year: int | None) -> bool:
    """후보 방영 시작 연도가 레코드의 활동 연도(미리보기 게시일 등)와 모순되지 않는가. 정보가 없으면 통과."""
    if not cand_year or not rec_years:
        return True
    return cand_year <= max(rec_years)


_GENERIC_CHANNELS = {"", "tving"}


def channel_compatible(a: str, b: str) -> bool:
    """두 채널 표기가 같은 작품일 수 있는가.
    같은 채널, 같은 계열(tvN / tvN STORY / tvN DRAMA …), 또는 한쪽이 'TVING·미상' 이고 다른 쪽이 tvN 계열/TVING·미상이면 양립."""
    a, b = _norm_channel(a), _norm_channel(b)
    if a == b:
        return True
    fam = lambda c: "tvn" if c.startswith("tvn") else c
    if fam(a) == fam(b):
        return True
    if a in _GENERIC_CHANNELS and (b in _GENERIC_CHANNELS or fam(b) == "tvn"):
        return True
    if b in _GENERIC_CHANNELS and (a in _GENERIC_CHANNELS or fam(a) == "tvn"):
        return True
    return False


def match_tvn_record(rec: dict, candidates: list[dict]) -> MatchResult:
    """tvN 레코드(parse_tvn_bundle 결과) ↔ 후보 목록."""
    years = sorted({int(e["posted_at"][:4]) for e in rec.get("episodes") or [] if e.get("posted_at")})
    return match_work({"title": rec.get("title", ""), "tving_code": rec.get("tving_code") or "",
                       "channel": "tvN", "years": years}, candidates)


def match_work(rec: dict, candidates: list[dict]) -> MatchResult:
    """출처와 무관한 일반 매칭. rec = {title, tving_code, channel, years[]} (years: 레코드가 활동한 연도들, 없으면 [])."""
    code = rec.get("tving_code") or ""

    # 1) 확정 키
    if code:
        hit = [c for c in candidates if c.get("tving_code") == code]
        if hit:
            return MatchResult("tving_code", 1.0, hit[0], f"TVING 코드 {code} 일치")

    # 2) 제목 + 채널 + 연도
    base, season = split_season(rec.get("title", ""))
    key = title_key(base)
    years = sorted(rec.get("years") or [])
    ok, rejected = [], []
    for c in candidates:
        cbase, cseason = split_season(c.get("title", ""))
        if title_key(cbase) != key:
            continue
        if not season_compatible(season, cseason):
            rejected.append((c, f"시즌 불일치({season}≠{cseason})"))
            continue
        if not channel_compatible(rec.get("channel", ""), c.get("channel", "")):
            rejected.append((c, f"채널 충돌({c.get('channel')})"))
            continue
        ccode = c.get("tving_code") or ""
        if code and ccode and ccode != code:
            rejected.append((c, f"TVING 코드 충돌({ccode}≠{code})"))
            continue
        if not _year_ok(years, c.get("year")):
            rejected.append((c, f"연도 모순({c.get('year')})"))
            continue
        ok.append(c)

    if len(ok) == 1:
        return MatchResult("title+channel+year", 0.8, ok[0], "제목·채널·연도 일치")
    if len(ok) > 1:
        return MatchResult("ambiguous", 0.0, None, f"후보 {len(ok)}개 — 자동 병합 안 함", alternatives=ok)
    if rejected:
        return MatchResult("rejected", 0.0, None, "; ".join(r for _, r in rejected),
                           alternatives=[c for c, _ in rejected])
    return MatchResult("none", 0.0, None, "일치하는 후보 없음")


_KDATE = r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"
_KRANGE_RE = re.compile(_KDATE + rf"(?:\s*~\s*(?:{_KDATE}|현재)?)?")


def season_period(text: str, season: int | None, first_ok: bool = False) -> dict | None:
    """위키 방영 기간 문자열에 시즌별 기간이 이어 적혀 있을 때 N번째 시즌의 기간을 고른다.

    '2024년 1월 17일 ~ 2024년 2월 7일 / 2026년 7월 3일 ~ 2026년 7월 25일' + 시즌 2 → 두 번째 기간.
    시즌 1·미표기(first_ok=False 일 때), 기간이 시즌 수보다 적은 경우, 시즌 2 이상에서 기간끼리 60일 미만으로 붙어 있는 경우(플랫폼별 동시 공개 등)는 None(= 건드리지 않음).
    반환: {'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'|'' , 'raw': '2026년 7월 3일 ~ 2026년 7월 25일'}"""
    from datetime import date
    if not text or not season or season < (1 if first_ok else 2):
        return None
    ranges = []
    for m in _KRANGE_RE.finditer(re.sub(r"\s+", " ", text)):
        g = m.groups()
        start = tuple(int(x) for x in g[0:3])
        end = tuple(int(x) for x in g[3:6]) if g[3] else None
        ongoing = "현재" in m.group(0)
        ranges.append((start, end, ongoing))
    if len(ranges) < season:
        return None
    try:
        starts = [date(*r[0]) for r in ranges]
    except ValueError:
        return None
    if season >= 2 and any((b - a).days < 60 for a, b in zip(starts, starts[1:])):
        return None
    (ys, ms, ds), end, ongoing = ranges[season - 1]
    kd = lambda y, m, d: f"{y}년 {m}월 {d}일"
    iso = lambda y, m, d: f"{y:04d}-{m:02d}-{d:02d}"
    raw = kd(ys, ms, ds) + (f" ~ {kd(*end)}" if end else " ~ 현재" if ongoing else "")
    return {"start": iso(ys, ms, ds), "end": iso(*end) if end else "", "raw": raw}
