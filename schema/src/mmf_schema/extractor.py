from __future__ import annotations

from abc import ABC, abstractmethod

from mmf_schema.record import MetadataRecord, Provenance


class BaseExtractor(ABC):
    """모든 모듈이 구현하는 인터페이스. 프레임워크는 이것만 알면 된다."""

    name: str = "base"
    version: str = "0.0.0"

    @abstractmethod
    def can_handle(self, source: str) -> bool:
        """이 추출기가 처리할 수 있는 입력인지 판단한다."""

    @abstractmethod
    def extract(self, source: str) -> list[MetadataRecord]:
        """입력을 받아 MetadataRecord 목록을 반환한다."""

    def provenance(self, source_uri: str) -> Provenance:
        return Provenance(
            source_uri=source_uri, extractor=self.name, extractor_version=self.version
        )
