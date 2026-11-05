"""국가법령정보센터 행정규칙 TXT의 머리말과 API 접두부를 정리한다."""
import re

from .common import (date_string, heading, identify_nodes, read_header,
                     split_sections)

# 규칙 모듈의 공통 인터페이스. heading·identify_nodes·split_sections는 common의 것을 그대로 쓴다.
__all__ = ['preprocess', 'split_sections', 'heading', 'identify_nodes']


def preprocess(source):
    """원문 행 위치를 유지하면서 수집 머리말과 첫 줄 API 정보만 분리한다."""
    rows = [{'source_line': i, 'text': text}
            for i, text in enumerate(source['raw_text'].splitlines(), 1)]
    metadata = read_header(rows)
    metadata['effective_date'] = date_string(metadata.get('effective_date'))
    kept, removed = [], []
    for row in rows:
        raw, value = row['text'], row['text'].strip()
        if kept:
            kept.append(row)
            continue
        if not value or value.startswith('#'):
            removed.append({**row, 'reason': 'header'})
            continue
        start = 0
        if metadata.get('serial') and value.startswith(metadata['serial'] + ' '):
            title = metadata['document_name']
            position = raw.find(title)
            start = position + len(title) if position >= 0 else 0
        match = re.search(r'제1(?:조\s*\(|편\s+|장\s+|절\s+)', raw[start:])
        if not match:
            raise ValueError(f"{row['source_line']}행: 첫 조문/제목을 찾지 못했습니다.")
        position = start + match.start()
        prefix = raw[:position]
        if prefix.strip():
            if not (metadata.get('serial') and prefix.split()[0] == metadata['serial']
                    and metadata['document_name'] in prefix):
                raise ValueError(f"{row['source_line']}행: 확인되지 않은 API 접두부")
            removed.append({**row, 'text': prefix, 'reason': 'api_prefix'})
        kept.append({**row, 'text': raw[position:]})
    if not kept:
        raise ValueError('본문이 없습니다.')
    return {'metadata': metadata, 'lines': kept, 'removed': removed}
