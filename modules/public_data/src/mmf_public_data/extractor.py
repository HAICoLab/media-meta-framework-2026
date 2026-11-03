from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml
from mmf_schema import BaseExtractor, MetadataRecord, SourceType

REQUIRED = ("id", "title", "source_url", "license")
DEFAULT_REGISTRY = Path(__file__).resolve().parents[2] / "registry.yaml"


def load_registry(path: str | Path = DEFAULT_REGISTRY) -> list[dict]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = data.get("datasets", [])
    seen: set[str] = set()
    for i, e in enumerate(entries):
        missing = [k for k in REQUIRED if not e.get(k)]
        if missing:
            raise ValueError(f"registry 항목 #{i} ({e.get('id')}) 필수 필드 누락: {missing}")
        if e["id"] in seen:
            raise ValueError(f"중복 id: {e['id']}")
        seen.add(e["id"])
    return entries


class PublicDataExtractor(BaseExtractor):
    name = "public_data"
    version = "0.1.0"

    def can_handle(self, source: str) -> bool:
        return source.lower().endswith((".yaml", ".yml")) and Path(source).is_file()

    def extract(self, source: str) -> list[MetadataRecord]:
        records = []
        for e in load_registry(source):
            published = e.get("published_at")
            records.append(
                MetadataRecord(
                    id=MetadataRecord.make_id(SourceType.PUBLIC_DATA, e["id"]),
                    source_type=SourceType.PUBLIC_DATA,
                    title=e["title"],
                    description=e.get("description"),
                    language=e.get("language"),
                    publisher=e.get("publisher"),
                    keywords=e.get("keywords", []),
                    license=e["license"],
                    published_at=date.fromisoformat(str(published)) if published else None,
                    provenance=self.provenance(e["source_url"]),
                    extra={
                        k: e[k]
                        for k in ("id", "format", "update_cycle", "local_path", "notes")
                        if k in e
                    },
                )
            )
        return records
