"""제작 국가 판별 (metadata_json 의 `origin`).

해외 작품을 '지우지 않고' 표시만 해 두기 위한 값이라, 확신이 없으면 unknown 으로 둔다.

  origin = {"group": "domestic" | "foreign" | "unknown",
            "country": "대한민국" | "일본" | … | null,
            "basis": "source_com2usn" | "wiki_category" | "tving_category" | "infobox" | "channel" | "cast_names" | "wiki_text" | null,
            "confidence": "high" | "medium" | "low" | null}

판별 근거 (위에서부터 먼저 걸리는 것):
  0. 출처가 com2usN                                                     (high)   — 한국 작품만 다루므로 항상 국내
  1. 위키 카테고리  '…대한민국의 텔레비전 …' / '…일본의 텔레비전 …'  (high)   — 국내 카테고리가 하나라도 있으면 국내
  2. TVING 분류   category='해외드라마' / genre='중국드라마' … (high), 예능·교양 → 국내 (medium)
  3. 위키 인포박스 '방송 국가'                                           (medium)
  4. 방송 채널     해외 방송사(채널차이나·중화TV·CBS 등) / 국내 방송사     (medium: tvN 수집·예능·교양 / low: 그 외 드라마)
                    — TVING·Wavve·넷플릭스 같은 플랫폼은 근거로 쓰지 않는다
  5. 출연진 이름에 띄어쓰기 (요코하마 류세이)                              (low, 해외 추정)
  6. 위키 본문     '장약윤(张若昀)' 처럼 한자 병기 이름이 여럿             (low, 해외 추정)
"""

import re

# 위키에서 쓰는 국가 표기 → 표시 이름
_COUNTRIES = {
    "대한민국": "대한민국", "한국": "대한민국", "남한": "대한민국",
    "일본": "일본", "중화인민공화국": "중국", "중국": "중국", "중화민국": "대만", "대만": "대만", "홍콩": "홍콩",
    "미국": "미국", "영국": "영국", "캐나다": "캐나다", "오스트레일리아": "호주", "호주": "호주", "뉴질랜드": "뉴질랜드",
    "프랑스": "프랑스", "독일": "독일", "스페인": "스페인", "이탈리아": "이탈리아", "러시아": "러시아", "터키": "튀르키예",
    "튀르키예": "튀르키예", "태국": "태국", "필리핀": "필리핀", "베트남": "베트남", "인도네시아": "인도네시아",
    "말레이시아": "말레이시아", "싱가포르": "싱가포르", "인도": "인도", "멕시코": "멕시코", "브라질": "브라질",
    "스웨덴": "스웨덴", "덴마크": "덴마크", "노르웨이": "노르웨이", "아일랜드": "아일랜드", "폴란드": "폴란드",
}
_KR = "대한민국"
_YEAR_PREFIX = re.compile(r"^(?:\d{4}년대|\d{4}년\s*(?:시작|종료)한)\s*")
_CAT_RE = re.compile(r"^(.+?)의\s+(?:텔레비전|웹\s?만화|웹툰|드라마|시트콤|예능|시리즈|리얼리티|토크|버라이어티)")

# 방송 채널 (플랫폼 TVING·Wavve·넷플릭스·디즈니+ 와 해외 판권을 사 와 방영하는 스튜디오지니·AsiaN·iMBC 등은 일부러 제외 — 해외 작품도 많다)
_DOMESTIC_CH = re.compile(
    r"^(?:tvn|mbc|kbs|sbs|jtbc|ena|채널a|ocn|mbn|tv조선|ebs|cj\s?enm|스튜디오드래곤|엠비씨|문화방송|"
    r"한국방송|서울방송|kbs\d|sbs\s?plus)", re.I)
_FOREIGN_CH = re.compile(
    r"채널차이나|중화tv|중천tv|cctv|hunan|iqiyi|youku|tencent|mango\s?tv|"
    r"^(?:cbs|nbc|abc|fox|hbo|lifetime|bbc|itv|nhk|tbs|fuji|ntv|tv\s?asahi|amc|showtime|netflix\s?japan)", re.I)
_HAN = re.compile(r"[\u4e00-\u9fff]")


def _countries_in(text: str) -> list:
    """'영국 미국 오스트레일리아', '중화민국, 대한민국' → 표시 이름 목록 (순서 유지, 중복 제거)."""
    out = []
    for tok in re.split(r"[\s,/·、ㆍ]+", str(text or "")):
        c = _COUNTRIES.get(tok.strip())
        if c and c not in out:
            out.append(c)
    return out


def _from_categories(cats) -> list:
    out = []
    for c in cats or []:
        s = _YEAR_PREFIX.sub("", str(c).strip())
        m = _CAT_RE.match(s)
        if not m:
            continue
        name = _COUNTRIES.get(m.group(1).strip())
        if name and name not in out:
            out.append(name)
    return out


def _pick(countries: list) -> tuple:
    """(group, country). 국내(대한민국)가 하나라도 섞여 있으면 국내 — 한중 합작·국내 리메이크는 국내로 본다."""
    if _KR in countries:
        return "domestic", _KR
    return "foreign", countries[0]


def _mk(group, country, basis, conf):
    return {"group": group, "country": country, "basis": basis, "confidence": conf}


def detect_origin(meta: dict, categories=None) -> dict:
    """metadata(wikipedia 포함) → origin dict. categories 를 주면 위키 카테고리로 쓴다."""
    meta = meta if isinstance(meta, dict) else {}
    wiki = meta.get("wikipedia") if isinstance(meta.get("wikipedia"), dict) else {}

    # 0) com2usN 은 한국 작품만 다루는 곳이라 출처만으로 국내로 확정한다 (위키 문서가 해외로 잘못 이어져도 따르지 않음)
    extra = meta.get("extra") if isinstance(meta.get("extra"), dict) else {}
    if (str(extra.get("source") or "").lower() == "com2usn"
            or str(meta.get("broadcaster") or "").lower() == "com2usn"
            or str(meta.get("channel") or "").lower() == "com2usn"):
        return _mk("domestic", _KR, "source_com2usn", "high")

    # 1) 위키 카테고리
    cats = categories if categories is not None else wiki.get("categories")
    found = _from_categories(cats)
    if found:
        g, c = _pick(found)
        return _mk(g, c, "wiki_category", "high")

    # 2) TVING 자체 분류 (해외드라마 > 중국드라마 등)
    tv = meta.get("tving") if isinstance(meta.get("tving"), dict) else {}
    tcat, tgenre = str(tv.get("category") or ""), str(tv.get("genre") or "")
    m = re.match(r"^(.+?)\s*(?:드라마|예능|시리즈|애니)$", tgenre)
    tc = _COUNTRIES.get(m.group(1).strip()) if m else None
    if tc:
        return _mk("domestic" if tc == _KR else "foreign", tc, "tving_category", "high")
    if "해외" in tcat or "해외" in tgenre:
        return _mk("foreign", None, "tving_category", "high")
    # TVING 에는 해외 예능·교양 분류가 없다(해외는 '드라마' 메뉴 아래 미국·영국·중국·일본·해외드라마뿐) → 예능·교양은 국내
    if tcat in ("예능", "교양"):
        return _mk("domestic", _KR, "tving_category", "medium")

    # 3) 인포박스 방송 국가
    found = _countries_in(meta.get("broadcast_country"))
    ib = wiki.get("infobox") if isinstance(wiki.get("infobox"), dict) else {}
    if not found:
        found = _countries_in(ib.get("방송 국가") or ib.get("국가") or "")
    if found:
        g, c = _pick(found)
        return _mk(g, c, "infobox", "medium")

    # 4) 방송 채널
    ch = str(meta.get("channel") or "").strip()
    if ch:
        if _FOREIGN_CH.search(ch):
            return _mk("foreign", None, "channel", "low")
        if _DOMESTIC_CH.search(ch):
            # tvN 공식 홈페이지에서 수집했거나 예능·교양이면 국내로 거의 확실하다. 드라마를 채널만으로 보면 약한 근거.
            solid = bool(meta.get("tvn")) or str(meta.get("content_type") or "") in ("variety", "culture")
            return _mk("domestic", _KR, "channel", "medium" if solid else "low")

    # 5) 출연진 이름에 띄어쓰기 — 한국 배우는 '김수현'처럼 붙여 쓰지만 일본·서양 배우는 '요코하마 류세이'처럼 성·이름을 띄운다
    cast = [str(x).strip() for x in (meta.get("cast") or []) if str(x).strip()][:6]
    if len(cast) >= 2 and sum(1 for n in cast if " " in n and not re.match(r"^(?:MC|마스터|패널|게스트|\w+\s?감독)\b", n)) >= max(2, len(cast) // 2):
        return _mk("foreign", None, "cast_names", "low")

    # 6) 위키 본문의 한자 병기 이름 (예: 장약윤(张若昀)) — 해외 작품의 전형적인 표기
    n_han = 0
    for sec in (wiki.get("sections") or []):
        stack = [sec]
        while stack:
            s = stack.pop()
            if not isinstance(s, dict):
                continue
            stack.extend(s.get("subsections") or [])
            for line in s.get("texts") or []:
                if re.search(r"[가-힣]\s?\([^)]*[\u4e00-\u9fff]{2,}[^)]*\)", str(line)):
                    n_han += 1
    if n_han >= 3:
        return _mk("foreign", None, "wiki_text", "low")

    return _mk("unknown", None, None, None)
