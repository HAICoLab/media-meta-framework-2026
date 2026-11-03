from pathlib import Path

from mmf_law_structurer import LawExtractor, parse_law_text

SAMPLE = Path(__file__).parent / "fixtures" / "sample_law.txt"


def test_hierarchy():
    root = parse_law_text(SAMPLE.read_text(encoding="utf-8"))
    assert root.number == "샘플방송법"
    assert [a.number for a in root.children] == ["제1조", "제2조", "제5조의2"]
    art2 = root.children[1]
    assert art2.heading == "정의"
    assert [p.number for p in art2.children] == ["①", "②"]
    items = art2.children[0].children
    assert [i.number for i in items] == ["1.", "2."]
    assert [s.number for s in items[1].children] == ["가.", "나."]
    assert art2.chapter == "제1장 총칙"


def test_multiline_and_addenda():
    root = parse_law_text(SAMPLE.read_text(encoding="utf-8"))
    art = root.children[2]
    assert art.chapter == "제2장 방송사업자"
    assert art.text.endswith("과징금을 부과할 수 있다.")
    assert "공포한 날" not in art.text  # 부칙은 본문에서 제외


def test_records_parent_links():
    recs = LawExtractor().extract(str(SAMPLE))
    by_id = {r.id: r for r in recs}
    assert recs[0].extra["level"] == "law" and recs[0].parent_id is None
    for r in recs[1:]:
        assert r.parent_id in by_id
    subitem = next(r for r in recs if r.extra["path"] == ["제2조", "①", "2.", "가."])
    assert subitem.text == "지상파방송사업"
