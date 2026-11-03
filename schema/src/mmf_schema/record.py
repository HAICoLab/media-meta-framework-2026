from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

SCHEMA_VERSION = "0.1.0"


class SourceType(str, Enum):
    WEB = "web"
    LAW = "law"
    PUBLIC_DATA = "public_data"


class Provenance(BaseModel):
    """레코드가 어디서, 언제, 어떤 추출기로 만들어졌는지."""

    source_uri: str = Field(description="원본 URL, 파일 경로 또는 데이터셋 식별자")
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    extractor: str
    extractor_version: str


class MetadataRecord(BaseModel):
    """세 모듈이 공통으로 내보내는 메타데이터 단위.

    - 공통 필드는 검색·필터링에 쓰이는 최소 집합만 둔다.
    - 모듈 고유 정보(법령의 조·항·호, 웹의 og:* 태그 등)는 `extra`에 넣는다.
    - 계층 구조(법령 → 조 → 항 → 호)는 `parent_id`로 표현한다.
    """

    schema_version: str = SCHEMA_VERSION
    id: str
    source_type: SourceType
    title: str | None = None
    description: str | None = None
    text: str | None = Field(default=None, description="본문 또는 조문 원문")
    language: str | None = None
    published_at: date | None = None
    publisher: str | None = None
    authors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    license: str | None = None
    parent_id: str | None = None
    provenance: Provenance
    extra: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def make_id(source_type: SourceType, *parts: str) -> str:
        """같은 입력이면 항상 같은 id가 나오도록 결정적으로 생성한다."""
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
        return f"{source_type.value}:{digest}"
