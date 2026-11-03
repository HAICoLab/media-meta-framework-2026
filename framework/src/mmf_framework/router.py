from __future__ import annotations

from mmf_law_structurer import LawExtractor
from mmf_public_data import PublicDataExtractor
from mmf_schema import BaseExtractor, MetadataRecord
from mmf_web_crawler import WebExtractor


class Router:
    """등록된 추출기 중 입력을 처리할 수 있는 첫 번째 추출기로 보낸다."""

    def __init__(self, extractors: list[BaseExtractor]) -> None:
        self.extractors = extractors

    def pick(self, source: str) -> BaseExtractor:
        for ex in self.extractors:
            if ex.can_handle(source):
                return ex
        raise ValueError(f"처리할 수 있는 모듈이 없습니다: {source}")

    def extract(self, source: str) -> list[MetadataRecord]:
        return self.pick(source).extract(source)


def default_router() -> Router:
    return Router([WebExtractor(), LawExtractor(), PublicDataExtractor()])
