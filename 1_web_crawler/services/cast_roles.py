"""출연자 ↔ 배역 정리 (metadata_json 의 `cast_roles`).

소스마다 '누가 무슨 역'이 흩어져 있다.
  - 위키 본문 '등장 인물/캐스트/출연진' 섹션: "김수현: 백현우 역(아역: …). 설명" 같은 문장 (+ 일부 표)
  - tvN `people`(kind=character): 배역 이름·나이/직업(brief)·인물 설명(detail)·이미지 — 배우 이름은 없음
  - TVING `people`(kind=cast): 배우 이름만 (배역 없음)
  - 공통 `cast`: 배우 이름 목록

이를 합쳐 `[{actor, role, role_note, group, brief, detail, wiki_description, image, sources}]` 한 목록으로 만든다.
값이 없으면 null. 위키 문장 형식이 맞지 않아 읽지 못한 줄은 버리고(추측하지 않음) 배우 이름만 남긴다.
"""

import re

_SECTION_RE = re.compile(r"등장\s?인물|출연진|출연자|출연|캐스트|배역")
# 배역 이름이 아니라 배우를 모으는 곳이 아닌 섹션(역대 출연자 표 등)은 줄 형태가 달라 자연히 걸러진다.
_NAME_RE = re.compile(r"^[가-힣A-Za-z][가-힣A-Za-z·\.\s\-]{0,19}$")
_ROLE_END_RE = re.compile(r"^(.+?)\s*역(?=$|[\s(\.,:;\-–—·/])")    # "백현우 역(…)" → 백현우
_AGE_RE = re.compile(r"\s*\(\s*\d+(?:\s*[→~\-]\s*\d+)?\s*(?:세)?\s*\)\s*$")
_BAD_ACTOR = re.compile(r"[0-9]|^제|화$|회$|^시즌")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _norm(s: str) -> str:
    return re.sub(r"[\s·\.\-]", "", str(s or "")).lower()


def _walk(sections, path=()):
    for s in sections or []:
        if not isinstance(s, dict):
            continue
        t = _clean(s.get("title"))
        yield path + (t,), s
        yield from _walk(s.get("subsections"), path + (t,))


_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _strip_groups(s: str, only_han: bool = False) -> str:
    """균형 잡힌 (…) 묶음을 지운다. only_han=True 면 한자가 든 묶음만 (예: 임완아(林婉儿) → 임완아)."""
    out, depth, buf = [], 0, []
    for ch in s:
        if ch == "(":
            if depth == 0:
                buf = []
            depth += 1
            buf.append(ch)
        elif ch == ")" and depth > 0:
            depth -= 1
            buf.append(ch)
            if depth == 0:
                grp = "".join(buf)
                if only_han and not _HAN_RE.search(grp):
                    out.append(grp)
                buf = []
        elif depth > 0:
            buf.append(ch)
        else:
            out.append(ch)
    if depth > 0:                       # 닫히지 않은 괄호는 그대로 둔다
        out.append("".join(buf))
    return _clean("".join(out))


def _find_depth0(s: str, pat: "re.Pattern"):
    """괄호 밖에서 처음 나오는 구분자(pat) 위치를 찾는다."""
    depth = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth == 0:
            m = pat.match(s, i)
            if m:
                return m
    return None


def _split_role_tail(rest: str):
    """'백현우 역(아역: 문성현). 설명' → (role, note, detail)."""
    rest = _clean(rest)
    m = _ROLE_END_RE.match(rest)
    if not m:
        return None
    role = _strip_groups(m.group(1), only_han=True)
    tail = _clean(rest[m.end():])
    note = None
    # 역 바로 뒤 괄호(아역·사망 표시·출연 회차)는 비고로
    notes = []
    while True:
        mm = re.match(r"^\(([^()]*)\)\s*", tail)
        if not mm:
            break
        notes.append(_clean(mm.group(1)))
        tail = tail[mm.end():]
    if notes:
        note = "; ".join(n for n in notes if n) or None
    tail = re.sub(r"^[\s\.\-–—:,]+", "", tail)
    return role, note, (tail or None)


_COLON = re.compile(r"\s*[:：]\s*")
_DASH = re.compile(r"\s+[-–—]\s+")


def _parse_line(line: str, known: set):
    """위키 한 줄 → (actor, role, note, detail) 또는 None."""
    line = _clean(line)
    if not line or len(line) > 400:
        return None
    # ① "배우 : 배역 역 …"  (배우 쪽의 '(한자)', '(소년기 : …)' 같은 괄호는 무시)
    m = _find_depth0(line, _COLON)
    if m:
        actor = _strip_groups(line[:m.start()])
        rest = line[m.end():]
        if actor and len(actor) <= 25 and _NAME_RE.match(actor) and not _BAD_ACTOR.search(actor):
            got = _split_role_tail(rest)
            if got:
                return (actor,) + got
            right = _strip_groups(rest, only_han=True)       # '역' 표기가 없으면 알려진 배우일 때만 짧은 배역명 인정
            if len(right) <= 20 and _norm(actor) in known:
                return actor, right, None, None
    # ② "배우 - 배역 역 …" 또는 일본 드라마식 "배역 (나이) - 배우"
    m = _find_depth0(line, _DASH)
    if m:
        left, right = _clean(line[:m.start()]), _clean(line[m.end():])
        left_name = _AGE_RE.sub("", _strip_groups(left, only_han=True))
        got = _split_role_tail(right)
        actor = _strip_groups(left)
        if got and actor and _NAME_RE.match(actor) and not _BAD_ACTOR.search(actor):
            return (actor,) + got
        right_name = _strip_groups(right, only_han=True)
        if _norm(right_name) in known:                              # 오른쪽 전체가 배우 → 왼쪽이 배역
            return right_name, left_name, None, None
        if _norm(left_name) in known and len(right_name) <= 30:     # 왼쪽이 배우 → 오른쪽이 배역
            return left_name, right_name, None, None
    return None


def _from_tables(sec: dict, known: set):
    """표에서 (배우, 배역) 뽑기. 열 머리글이 '배우/출연' + '배역/등장 인물' 인 표, 또는 한 인물 카드 표(배우 | 이름)만."""
    out = []
    for tb in sec.get("tables") or []:
        rows = [[_clean(c.get("text")) for c in r] for r in (tb.get("rows") or [])]
        if not rows:
            continue
        head = rows[0]
        ai = next((i for i, h in enumerate(head) if re.search(r"배우|출연", h) and "성우" not in h and "기간" not in h and "회차" not in h), None)
        ri = next((i for i, h in enumerate(head) if re.search(r"배역|극중|등장\s?인물|캐릭터|역할", h)), None)
        if ai is not None and ri is not None and ai != ri:
            for r in rows[1:]:
                if len(r) > max(ai, ri) and r[ai] and r[ri]:
                    out.append((r[ai], r[ri], None, None))
            continue
        # 인물 카드 표: 첫 칸이 배역 이름, '배우' 행에 배우 이름
        title = rows[0][0] if rows[0] else ""
        for r in rows:
            if len(r) >= 2 and r[0] == "배우" and r[1] and title and title != "배우":
                out.append((r[1], title, None, None))
                break
    return out


def build_cast_roles(meta: dict) -> list:
    """metadata(wikipedia/tving/tvn/cast 포함)에서 배우↔배역 목록을 만든다."""
    meta = meta if isinstance(meta, dict) else {}
    wiki = meta.get("wikipedia") if isinstance(meta.get("wikipedia"), dict) else {}
    tving = meta.get("tving") if isinstance(meta.get("tving"), dict) else {}
    tvn = meta.get("tvn") if isinstance(meta.get("tvn"), dict) else {}

    # 알려진 배우 이름 (TVING 출연, 공통 cast, 위키 인포박스 출연자)
    names = []
    for n in (meta.get("cast") or []):
        names.append(_clean(re.sub(r"\s*外$", "", str(n))))
    for p in (tving.get("people") or []):
        if isinstance(p, dict) and p.get("kind") == "cast":
            names.append(_clean(p.get("name")))
    ibc = ((wiki.get("infobox") or {}).get("출연자") if isinstance(wiki.get("infobox"), dict) else "") or ""
    names += [_clean(x) for x in re.split(r"[,\n、]", re.sub(r"\s*外", "", str(ibc)))]
    names = [n for n in names if n]
    known = {_norm(n) for n in names}

    entries: list = []          # 순서 보존
    by_actor: dict = {}

    def add(actor, role=None, note=None, detail=None, group=None, source="wikipedia", brief=None, image=None):
        actor = _clean(actor) or None
        role = _clean(role) or None
        if not actor and not role:
            return None
        e = None
        if actor:
            for cand in by_actor.get(_norm(actor), []):
                if cand["role"] is None or role is None or _norm(cand["role"]) == _norm(role):
                    e = cand
                    break
        if e is None:
            e = {"actor": actor, "role": role, "role_note": None, "group": group, "brief": None,
                 "detail": None, "wiki_description": None, "image": None, "sources": []}
            entries.append(e)
            if actor:
                by_actor.setdefault(_norm(actor), []).append(e)
        if role and not e["role"]:
            e["role"] = role
        # 위키 문장 속 한 줄 설명은 wiki_description, tvN 의 긴 인물 설명은 detail 로 따로 둔다
        dkey = "wiki_description" if source == "wikipedia" else "detail"
        for k, v in (("role_note", note), ("group", group), ("brief", brief), (dkey, detail), ("image", image)):
            if v and not e[k]:
                e[k] = _clean(v) if k not in ("detail", "wiki_description") else str(v).strip()
        if source not in e["sources"]:
            e["sources"].append(source)
        return e

    # 1) 위키 본문
    seen_sections = set()
    for path, sec in _walk(wiki.get("sections")):
        if not path or not _SECTION_RE.search(path[0]):
            continue
        if id(sec) in seen_sections:
            continue
        seen_sections.add(id(sec))
        group = path[-1] if len(path) > 1 else None
        for line in sec.get("texts") or []:
            got = _parse_line(line, known)
            if got:
                actor, role, note, detail = got
                add(actor, role, note, detail, group=group, source="wikipedia")
        for actor, role, note, detail in _from_tables(sec, known):
            add(actor, role, note, detail, group=group, source="wikipedia")

    # 2) tvN 배역 설명 붙이기 (배역 이름으로 연결, 못 붙이면 배우 없는 배역으로 추가)
    for p in (tvn.get("people") or []):
        if not isinstance(p, dict) or p.get("kind") != "character" or not p.get("name"):
            continue
        cname = _clean(p["name"])
        target = None
        for e in entries:
            if not e["role"]:
                continue
            a, b = _norm(e["role"]), _norm(cname)
            if a == b or (len(b) >= 2 and (a.startswith(b) or a.endswith(b) or b in a.split())) :
                target = e
                break
            if len(b) >= 3 and b in a:
                target = e
                break
        if target is not None:
            add(target["actor"], target["role"], None, p.get("detail"), group=p.get("group"),
                source="tvn", brief=p.get("brief"), image=p.get("image"))
        else:
            e = {"actor": None, "role": cname, "role_note": None, "group": _clean(p.get("group")) or None,
                 "brief": _clean(p.get("brief")) or None, "detail": (str(p.get("detail")).strip() or None) if p.get("detail") else None,
                 "wiki_description": None, "image": p.get("image") or None, "sources": ["tvn"]}
            entries.append(e)

    # 2-1) tvN 출연진(kind=cast): 예능·교양의 출연자/MC 소개(직함·소개글·사진). 배역이 없어 배우 칸에 이름만 넣고 설명을 붙인다
    for p in (tvn.get("people") or []):
        if not isinstance(p, dict) or p.get("kind") != "cast" or not _clean(p.get("name")):
            continue
        add(p["name"], None, None, p.get("detail"), group=p.get("group"),
            source="tvn", brief=p.get("brief"), image=p.get("image"))

    # 3) 배우 이름만 아는 사람 (TVING·공통 cast) — 이미 있으면 출처만 추가
    for n in names:
        hit = by_actor.get(_norm(n))
        if hit:
            if "tving" not in hit[0]["sources"] and any(
                    isinstance(p, dict) and p.get("kind") == "cast" and _norm(p.get("name")) == _norm(n)
                    for p in (tving.get("people") or [])):
                hit[0]["sources"].append("tving")
            continue
        src = "tving" if any(isinstance(p, dict) and _norm(p.get("name")) == _norm(n) for p in (tving.get("people") or [])) else "cast"
        add(n, None, source=src)

    # 배우-배역이 연결된 항목을 앞에, 그다음 배우만, 마지막으로 배우 없는 배역 (각 그룹 안에서는 발견 순서 유지)
    def rank(e):
        return 0 if (e["actor"] and e["role"]) else (1 if e["actor"] else 2)
    return sorted(entries, key=rank)
