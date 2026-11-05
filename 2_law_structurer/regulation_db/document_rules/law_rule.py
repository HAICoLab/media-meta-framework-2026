"""국가법령정보센터 법령 TXT의 반복 API 정보와 번호 메타데이터를 정리한다."""
import re

from .common import (MARKERS, article_label, date_string, heading, identify_nodes,
                     read_header, split_sections, supplement_start)

# 규칙 모듈의 공통 인터페이스. heading·identify_nodes·split_sections는 common의 것을 그대로 쓴다.
__all__ = ['preprocess', 'split_sections', 'heading', 'identify_nodes']


def _content_line(value):
    return heading(value) is not None or article_label(value) is not None


def _duplicate_marker_end(rows, index, limit):
    value = rows[index]['text'].strip()
    if not re.fullmatch('[' + MARKERS + r']|\d+\.|[가-힣]\.', value):
        return None
    j = index + 1
    suffix = None
    if value[:-1].isdigit() and j < limit and re.fullmatch(r'\d+', rows[j]['text'].strip()):
        suffix = rows[j]['text'].strip()
        j += 1
    # 번호와 실제 문장 사이에 API의 개정 종류·날짜가 끼는 형식이 있다.
    if j + 1 < limit and rows[j]['text'].strip() in {'개정', '신설', '전문개정', '제목개정'}:
        if re.fullmatch(r'[\d.,\s]+', rows[j + 1]['text'].strip()):
            j += 2
    marker = (value[:-1] + '의' + suffix + '.') if suffix else value
    if j < limit:
        following = rows[j]['text'].strip()
        if following.startswith(marker) and following != marker:
            return j
    return None


def preprocess(source):
    """법령 메타데이터만 제외하고 실제 조문·부칙·첨부의 원문 행을 보존한다."""
    rows = [{'source_line': i, 'text': text}
            for i, text in enumerate(source['raw_text'].splitlines(), 1)]
    metadata = read_header(rows)
    first = next((i for i, row in enumerate(rows) if _content_line(row['text'].strip())), None)
    if first is None:
        raise ValueError('법령의 첫 조문/제목을 찾지 못했습니다.')
    dates = [rows[i]['text'].strip() for i in range(first - 1)
             if re.fullmatch(r'\d{8}', rows[i]['text'].strip())
             and rows[i + 1]['text'].strip() in {'제정', '일부개정', '전부개정', '타법개정'}]
    if len(set(dates)) != 1:
        raise ValueError('법령 API 머리말에서 시행일을 확정할 수 없습니다.')
    metadata['effective_date'] = date_string(dates[0])
    body_end = next((i for i in range(first, len(rows)) if supplement_start(rows, i)), len(rows))
    removed = [{**row, 'reason': 'law_api_header'} for row in rows[:first]]
    kept, i = [], first
    while i < body_end:
        value = rows[i]['text'].strip()
        if re.fullmatch(r'\d+', value):
            marker = next((j for j in range(i + 1, min(i + 3, body_end))
                           if rows[j]['text'].strip() in {'조문', '전문'}), None)
            if marker is not None:
                end = next((j for j in range(marker + 1, min(marker + 12, body_end))
                            if _content_line(rows[j]['text'].strip())), None)
                if end is None:
                    raise ValueError(f"{rows[i]['source_line']}행: 불완전한 조문 API 정보")
                removed.extend({**row, 'reason': 'article_api_metadata'} for row in rows[i:end])
                i = end
                continue
        end = _duplicate_marker_end(rows, i, body_end)
        if end is not None:
            removed.extend({**row, 'reason': 'duplicate_number_metadata'} for row in rows[i:end])
            i = end
            continue
        kept.append(rows[i])
        i += 1
    kept.extend(rows[body_end:])
    return {'metadata': metadata, 'lines': kept, 'removed': removed}
