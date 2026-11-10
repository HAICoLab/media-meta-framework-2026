"""위키피디아 '등장 인물' 섹션에서 배역 ↔ 배우를 뽑아 tvN 등장인물(drama_people.kind='character')에 연결한다.

위키 본문은 대체로 한 줄이 `배우 : 배역 (나이) 역 - 설명` 모양이다.
  임시완 : 윤이준 (32세) 역 - 태강그룹 차기 후계자      → 배우=임시완, 배역=윤이준
  김혜준 : 정지안 역 (아역 : 안세빈, 조시연)              → 배우=김혜준, 배역=정지안
  미상 : 제리 (?세) 역                                    → 배우 불명이라 연결하지 않는다
tvN 쪽에는 배역명만 있으므로, 배역명이 같은 줄을 찾아 actor_name / actor_source='wiki' 로 채운다.

extract_cast_roles(sections) -> [{actor, role, section}]   (순수 함수)
link_actors(db, drama_id)    -> 연결한 인물 수             (DB: dramas.wiki_sections_json, drama_people)
"""
from __future__ import annotations

import json
import re

_SECTION_RE = re.compile(r"등장\s*인물|출연\s*(?:자|진)|배역|캐릭터|주요\s*인물|출연")
_LINE_RE = re.compile(r"^\s*(?P<actor>[^:：\n]{1,40}?)\s*[:：]\s*(?P<role>[^\n]+?)\s*역(?=$|[\s(（\-–—,·:.])")
_UNKNOWN_ACTORS = {"미상", "불명", "미정", "미공개", "?"}
_QUOTES = "\"'“”‘’「」『』"


def _clean_role(role: str) -> str:
    r = re.sub(r"[（(][^）)]*[）)]", " ", role)             # (32세) (아역: …) 제거
    r = r.translate({ord(c): None for c in _QUOTES})
    r = re.sub(r"\s+", " ", r).strip(" ,/·-")
    return r


def role_key(s: str) -> str:
    return re.sub(r"[\s\-_·,./]+", "", _clean_role(s)).lower()


def _walk(sections, in_cast: bool = False):
    for sec in sections or []:
        if not isinstance(sec, dict):
            continue
        here = in_cast or bool(_SECTION_RE.search(sec.get("title") or ""))
        if here:
            for t in sec.get("texts") or []:
                yield sec.get("title") or "", t
        yield from _walk(sec.get("subsections"), here)


def extract_cast_roles(sections: list) -> list[dict]:
    out, seen = [], set()
    for title, line in _walk(sections):
        m = _LINE_RE.match(line or "")
        if not m:
            continue
        actor = re.sub(r"\s+", " ", m["actor"]).strip()
        role = _clean_role(m["role"])
        if not actor or not role or actor in _UNKNOWN_ACTORS or len(role) > 40:
            continue
        key = (actor, role)
        if key in seen:
            continue
        seen.add(key)
        rest = (line or "")[m.end():]
        desc = re.sub(r"^[\s\-–—:：.]+", "", re.sub(r"^\s*[（(][^）)]*[）)]", "", rest)).strip()   # '역' 뒤 설명 ('- 현준의 매니저…')
        out.append({"actor": actor, "role": role, "section": title, "desc": desc,
                    "raw_role": m["role"]})
    return out


_AGE_PAREN_RE = re.compile(r"\s*[（(]\s*(?:약\s*)?[\d?]+\s*(?:세|대|살)?[^）)]*[）)]\s*$")


def split_character_name(name: str) -> tuple[str, str]:
    """tvN 배역명 '도깨비(김신), 939세' / '임현준 (40대)' → (배역명, 나이·부가설명). 없으면 부가설명은 ''."""
    n = re.sub(r"\s+", " ", name or "").strip()
    extra = ""
    if re.search(r"[,，]", n):
        n, extra = (x.strip() for x in re.split(r"[,，]", n, maxsplit=1))
    m = _AGE_PAREN_RE.search(n)
    if m:
        extra = (m.group(0).strip(" ()（）") + (", " + extra if extra else "")).strip(", ")
        n = n[:m.start()].strip()
    return n, extra


def _aliases_tvn(name: str) -> set[str]:
    """'도깨비(김신)' → {도깨비, 김신}. 나이/설명 꼬리는 버린다."""
    base, _ = split_character_name(name)
    keys = {role_key(base)}
    keys.add(role_key(re.sub(r"[（(][^）)]*[）)]", " ", base)))
    for inner in re.findall(r"[（(]([^）)]+)[）)]", base):
        keys.add(role_key(inner))
    return {k for k in keys if k}


def _aliases_wiki(role: dict) -> set[str]:
    """'저승사자 / 왕여' → {저승사자, 왕여, 저승사자왕여}. 위키 쪽은 한 줄에 여러 이름을 '/'로 적는 경우가 많다."""
    raw = re.sub(r"[（(][^）)]*[）)]", " ", role.get("raw_role") or role["role"])
    keys = {role_key(role["role"])}
    for part in re.split(r"[/,·]", raw):
        keys.add(role_key(part))
    return {k for k in keys if k}


def _desc_key(t: str) -> str:
    return re.sub(r"[\s.,·\-–—~'\"“”‘’()（）\[\]]+", "", t or "")


def _desc_score(r: dict, brief: str) -> int:
    dk, bk = _desc_key(r.get("desc")), _desc_key(brief)
    if len(dk) >= 6 and len(bk) >= 6 and (bk in dk or dk in bk):
        return min(len(dk), len(bk))
    # '남, 30대, IT 기업 CEO' 처럼 일부 조각만 겹치는 경우: 쉼표 조각 중 5자 이상이 위키 설명에 포함
    return max((len(pc) for pc in (_desc_key(x) for x in re.split(r"[,.]", brief or "")) if len(pc) >= 5 and pc in dk),
               default=0)


def _pick_by_desc(cands: list[dict], brief: str) -> dict | None:
    """tvN 설명(brief)과 위키 설명이 가장 많이 겹치는 한 줄. 동점이거나 겹침이 없으면 None."""
    scored = sorted(((_desc_score(r, brief), i) for i, r in enumerate(cands)), reverse=True)
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return cands[scored[0][1]]


def match_roles(characters, roles: list[dict]) -> dict[str, dict]:
    """tvN 배역명 → 위키 줄.
    characters: 배역명 문자열 목록, 또는 (배역명, 설명) 목록.
    1) 이름(별칭 포함)이 정확히 같은 줄  2) 한쪽이 다른 쪽을 포함(2글자 이상)하고 후보가 하나일 때
    3) 후보가 여러 줄이면 tvN 설명과 위키 설명이 가장 겹치는 줄  4) 이름이 달라도(황대표↔황지순) 설명이 사실상 같고,
    아직 다른 배역에 쓰이지 않은 위키 줄이 하나뿐일 때. 모호하면 연결하지 않는다."""
    result: dict[str, dict] = {}
    used: set[int] = set()
    wiki_alias = [(r, _aliases_wiki(r)) for r in roles]
    chars = [(ch, "") if isinstance(ch, str) else (ch[0], ch[1] or "") for ch in characters]
    leftover = []
    for name, brief in chars:
        mine = _aliases_tvn(name)
        if not mine:
            continue
        exact = [r for r, al in wiki_alias if al & mine]
        if exact:
            hit = exact[0] if len({r["actor"] for r in exact}) == 1 else (_pick_by_desc(exact, brief) or exact[0])
        else:
            loose = [r for r, al in wiki_alias
                     if any(len(a) >= 2 and len(m) >= 2 and (a in m or m in a) for a in al for m in mine)]
            hit = loose[0] if len(loose) == 1 else (_pick_by_desc(loose, brief) if loose else None)
            if hit is None and not loose:
                leftover.append((name, brief))
        if hit:
            result[name] = hit
            used.add(id(hit))
    for name, brief in leftover:
        pick = _pick_by_desc([r for r in roles if id(r) not in used and r.get("desc")], brief)
        if pick:
            result[name] = pick
            used.add(id(pick))
    return result


def extract_wiki_characters(sections: list) -> list[dict]:
    """위키 '등장 인물' 섹션 → 극중 인물 목록 [{group, role, actor, brief, detail}].
    '배우 : 배역 역 - 한 줄 설명' 줄이 한 명이고, 바로 뒤에 이어지는 일반 문단은 그 인물의 긴 설명(detail)으로 붙인다.
    소제목(주요 인물/○○ 식구들 …)은 그룹명으로 쓴다."""
    out: list[dict] = []
    seen: set[tuple] = set()

    def walk(secs, group="", in_cast=False):
        for sec in secs or []:
            if not isinstance(sec, dict):
                continue
            title = sec.get("title") or ""
            here = in_cast or bool(_SECTION_RE.search(title))
            g = title if (in_cast and title) else group
            if here:
                last = None
                for line in sec.get("texts") or []:
                    m = _LINE_RE.match(line or "")
                    if m:
                        actor = re.sub(r"\s+", " ", m["actor"]).strip()
                        role = _clean_role(m["role"])
                        if not actor or not role or actor in _UNKNOWN_ACTORS or len(role) > 40 or (actor, role) in seen:
                            last = None
                            continue
                        seen.add((actor, role))
                        rest = (line or "")[m.end():]
                        desc = re.sub(r"^[\s\-–—:：.]+", "", re.sub(r"^\s*[（(][^）)]*[）)]", "", rest)).strip()
                        last = {"group": g, "role": role, "actor": actor, "brief": desc, "detail": ""}
                        out.append(last)
                    elif last is not None and line and ":" not in line[:20] and "：" not in line[:20]:
                        last["detail"] = (last["detail"] + "\n" + line.strip()).strip()
            walk(sec.get("subsections"), g, here)

    walk(sections)
    return out


async def build_wiki_characters(db, drama_id: int, sections: list) -> int:
    """tvN 등 공식 출처에 등장인물이 없는 작품(TVING 단독 등)에 위키 '등장 인물'로 인물 목록을 채운다.
    공식 출처의 인물이 있으면 아무것도 하지 않는다. 위키에서 온 인물은 다시 실행하면 새로 만든다."""
    cur = await db.execute("SELECT COUNT(*) FROM drama_people WHERE drama_id=? AND kind='character' AND source!='wiki'", (drama_id,))
    if (await cur.fetchone())[0]:
        return 0
    chars = extract_wiki_characters(sections)
    await db.execute("DELETE FROM drama_people WHERE drama_id=? AND kind='character' AND source='wiki'", (drama_id,))
    for i, c in enumerate(chars):
        await db.execute(
            "INSERT OR REPLACE INTO drama_people (drama_id, source, kind, group_name, name, brief, detail, image, ord, actor_name, actor_source) "
            "VALUES (?, 'wiki', 'character', ?, ?, ?, ?, '', ?, ?, 'wiki')",
            (drama_id, c["group"], c["role"], c["brief"], c["detail"], i, c["actor"]))
    return len(chars)


async def link_actors(db, drama_id: int) -> int:
    """drama_id 의 tvN 등장인물에 위키 배우를 연결한다. 이미 사람이 채운 값(actor_source != 'wiki')은 덮어쓰지 않는다."""
    cur = await db.execute("SELECT wiki_sections_json FROM dramas WHERE id=?", (drama_id,))
    row = await cur.fetchone()
    if not row or not row[0]:
        return 0
    try:
        sections = json.loads(row[0])
    except Exception:
        return 0
    built = await build_wiki_characters(db, drama_id, sections)      # 공식 출처 인물이 없으면 위키로 채운다
    cur = await db.execute(
        "SELECT id, name, brief FROM drama_people WHERE drama_id=? AND kind='character' AND source!='wiki' "
        "AND (actor_source IS NULL OR actor_source='' OR actor_source='wiki')", (drama_id,))
    chars = await cur.fetchall()
    if built and not chars:
        return built
    if not chars:
        return 0
    matched = match_roles([(n, b) for _, n, b in chars], extract_cast_roles(sections))
    n = 0
    for pid, name, _brief in chars:
        r = matched.get(name)
        if r:
            await db.execute("UPDATE drama_people SET actor_name=?, actor_source='wiki' WHERE id=?", (r["actor"], pid))
            n += 1
    return n
