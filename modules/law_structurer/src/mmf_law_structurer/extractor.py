from __future__ import annotations

from pathlib import Path

from mmf_schema import BaseExtractor, MetadataRecord, SourceType

from mmf_law_structurer.parser import parse_law_text


class LawExtractor(BaseExtractor):
    name = "law_structurer"
    version = "0.1.0"

    def can_handle(self, source: str) -> bool:
        return source.lower().endswith(".txt") and Path(source).is_file()

    def extract(self, source: str) -> list[MetadataRecord]:
        raw = Path(source).read_text(encoding="utf-8")
        return self.from_text(raw, source_uri=source)

    def from_text(self, raw: str, source_uri: str) -> list[MetadataRecord]:
        root = parse_law_text(raw)
        law_name = root.number
        records: list[MetadataRecord] = []
        for node, path, parent_path in root.walk():
            title = law_name if node.level == "law" else f"{law_name} {' '.join(path[1:])}"
            if node.heading:
                title += f"({node.heading})"
            records.append(
                MetadataRecord(
                    id=MetadataRecord.make_id(SourceType.LAW, *path),
                    source_type=SourceType.LAW,
                    title=title,
                    text=node.text or None,
                    language="ko",
                    parent_id=(
                        MetadataRecord.make_id(SourceType.LAW, *parent_path)
                        if parent_path
                        else None
                    ),
                    provenance=self.provenance(source_uri),
                    extra={
                        "law_name": law_name,
                        "level": node.level,
                        "number": node.number,
                        "heading": node.heading,
                        "chapter": node.chapter,
                        "section": node.section,
                        "path": list(path[1:]),
                    },
                )
            )
        return records
