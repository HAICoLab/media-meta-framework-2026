import json
from pathlib import Path

import pytest
from mmf_framework import default_router
from mmf_framework.cli import main

ROOT = Path(__file__).resolve().parents[2]
LAW = ROOT / "modules/law_structurer/tests/fixtures/sample_law.txt"
REGISTRY = ROOT / "modules/public_data/registry.yaml"


@pytest.mark.parametrize(
    "source, expected",
    [
        ("https://example.com", "web_crawler"),
        (str(LAW), "law_structurer"),
        (str(REGISTRY), "public_data"),
    ],
)
def test_routing(source, expected):
    assert default_router().pick(source).name == expected


def test_unknown_source():
    with pytest.raises(ValueError):
        default_router().pick("not-a-source")


def test_cli_jsonl(tmp_path):
    out = tmp_path / "out.jsonl"
    assert main([str(LAW), str(REGISTRY), "-o", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert {r["source_type"] for r in rows} == {"law", "public_data"}
