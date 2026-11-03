import pytest
from mmf_public_data import PublicDataExtractor, load_registry
from mmf_public_data.extractor import DEFAULT_REGISTRY


def test_registry_is_valid():
    assert load_registry(DEFAULT_REGISTRY)


def test_extract_registry():
    recs = PublicDataExtractor().extract(str(DEFAULT_REGISTRY))
    assert all(r.license for r in recs)


def test_missing_field_rejected(tmp_path):
    bad = tmp_path / "r.yaml"
    bad.write_text("datasets:\n  - id: x\n    title: t\n", encoding="utf-8")
    with pytest.raises(ValueError, match="필수 필드"):
        load_registry(bad)
