from datetime import date
from pathlib import Path

from mmf_web_crawler import WebExtractor, parse_html

HTML = (Path(__file__).parent / "fixtures" / "sample.html").read_text(encoding="utf-8")


def test_parse_html_fields():
    f = parse_html(HTML, "https://example.com/news/1?ref=x")
    assert f["title"] == "OG 샘플 기사 제목"
    assert f["keywords"] == ["방송", "미디어", "메타데이터"]
    assert f["language"] == "ko"
    assert f["published_at"] == date(2026, 10, 1)
    assert f["publisher"] == "샘플뉴스"
    assert f["extra"]["canonical_url"] == "https://example.com/news/1"


def test_extract_without_network(monkeypatch):
    ex = WebExtractor()
    monkeypatch.setattr(ex, "fetch", lambda url: HTML)
    [rec] = ex.extract("https://example.com/news/1")
    assert rec.source_type.value == "web"
    assert rec.provenance.extractor == "web_crawler"
