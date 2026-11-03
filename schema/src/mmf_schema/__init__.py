"""공통 메타데이터 스키마.

이 패키지가 모듈 간 계약(contract)이다. 필드를 바꾸면 SCHEMA_VERSION을 올리고
docs/schema-changelog.md에 기록한다.
"""

from mmf_schema.extractor import BaseExtractor
from mmf_schema.record import SCHEMA_VERSION, MetadataRecord, Provenance, SourceType

__all__ = ["SCHEMA_VERSION", "BaseExtractor", "MetadataRecord", "Provenance", "SourceType"]
