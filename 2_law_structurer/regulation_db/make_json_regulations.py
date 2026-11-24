"""규정 TXT를 문서별 중첩 JSON으로 변환한다.

실행: python make_json_regulations.py data output/regulations
출처 머리말로 형식 판별 → 영역 분리 → 원문 보정 → 조·항·호·목 구성 → 조항 ID → 표 변환 → 원문 대조 검증 → 저장.
조항 ID 앞머리와 원문 보정 표는 document_rules/documents.py에 있다.
"""
import argparse
from collections import Counter
from datetime import date
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

if __package__:
    from .document_rules import administrative_rule, law_rule, common, documents
    from .table_processor import process_tables
else:
    from document_rules import administrative_rule, law_rule, common, documents
    from table_processor import process_tables


def _compact(value):
    return re.sub(r'\s+', '', value)


# 계층 순서. 각 노드는 자기보다 아래 단계의 노드만 자식으로 가질 수 있다(예: 호 없이 목).
LEVELS = ['article', 'paragraph', 'item', 'subitem']
CHILD_TYPES = {kind: set(LEVELS[i + 1:]) for i, kind in enumerate(LEVELS)}


def read_source(path):
    path = Path(path)
    return {'file': unicodedata.normalize('NFC', path.name),
            'raw_text': path.read_bytes().decode('utf-8-sig')}


def _is_iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def select_rules(text):
    """문서명이 아닌 출처 머리말로 입력 형식을 선택한다."""
    header = '\n'.join(line.strip() for line in text.splitlines()[:12] if line.lstrip().startswith('#'))
    law = '(target=law)' in header
    admin = '(target=admrul)' in header or '행정규칙집 수록' in header
    if law == admin:
        raise ValueError('출처 머리말로 법령/행정규칙을 구분할 수 없습니다.')
    return law_rule if law else administrative_rule


def _set_parent(parents, key, label):
    order = ['part', 'chapter', 'section']
    for lower in order[order.index(key):]:
        parents.pop(lower, None)
    parents[key] = label


def build_nodes(rows, rules):
    """상위 제목은 parents에, 조·항·호·목은 children에 저장한다."""
    articles, stack, parents, reviews = [], [], {}, []
    for row in rows:
        value = row['text'].strip()
        if not value:
            continue
        title = rules.heading(value)
        if title:
            _set_parent(parents, *title)
            stack.clear()
            continue
        if common.ANNOTATION.fullmatch(value):
            if not articles:
                raise ValueError(f"{row['source_line']}행: 소속 조문 없는 주석")
            articles[-1].setdefault('annotations', []).append(
                {'source_line': row['source_line'], 'text': value})
            continue
        nodes = rules.identify_nodes(value)
        if not nodes:
            if not stack:
                raise ValueError(f"{row['source_line']}행: 소속 없는 본문 {value[:60]}")
            stack[-1]['text'] += '\n' + value
            reviews.append({'source_line': row['source_line'], 'label': articles[-1]['label'],
                            'reason': '번호 없는 문장: 직전 노드에 이어 붙임'})
            continue
        for node in nodes:
            kind = node['type']
            while stack and LEVELS.index(stack[-1]['type']) >= LEVELS.index(kind):
                stack.pop()
            if kind == 'article':
                if parents:
                    node = {'parents': parents.copy(), **node}
                articles.append(node)
            else:
                if not stack or kind not in CHILD_TYPES[stack[-1]['type']]:
                    raise ValueError(f"{row['source_line']}행: {node['label']}의 부모를 찾을 수 없습니다.")
                if kind == 'subitem' and stack[-1]['type'] != 'item':
                    reviews.append({'source_line': row['source_line'], 'label': articles[-1]['label'],
                                    'reason': '호 없이 시작하는 목: 상위 항·조 바로 아래에 둠'})
                stack[-1]['children'].append(node)
                pattern = {'paragraph': common.PARAGRAPH, 'item': common.ITEM,
                           'subitem': common.SUBITEM}[kind]
                if not pattern.match(node['text'])[2].strip():
                    reviews.append({'source_line': row['source_line'], 'label': node['label'],
                                    'reason': '번호 뒤 내용 없음: 원문 수집 누락/삭제 여부 확인 필요'})
            stack.append(node)
    return articles, reviews


def build_records(source, prepared, sections, rules):
    """영역별로 레코드를 만든다. 본문은 조문 계층으로, 부칙·별표는 표를 변환한 블록으로 만든다."""
    metadata = prepared['metadata']
    document_id = metadata.get('serial')
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError('문서 식별자가 없습니다.')
    id_prefix = documents.prefix(metadata['document_name'])
    records, reviews, removed = [], [], []
    origin = {'file': source['file'], 'effective_date': metadata['effective_date']}
    for section in sections:
        envelope = {'document_id': document_id, 'document_name': metadata['document_name'],
                    'source': origin.copy(), 'region': section['region'],
                    'region_label': section['region_label']}
        if section['region'] == 'body':
            rows, notes = documents.correct(metadata['document_name'], section['lines'])
            nodes, warnings = build_nodes(rows, rules)
            records.extend(documents.assign_ids({**envelope, **node}, id_prefix) for node in nodes)
            reviews.extend({**note, 'label': '본문'} for note in notes)
            reviews.extend(warnings)
        else:
            rows, label = section['lines'], section['region_label']
            if section['region'] == 'appendix':
                label, rows, discarded = common.clean_appendix(section)
                envelope['region_label'] = label
                removed.extend(discarded)
            # 앞쪽 빈 줄을 먼저 떼어, 표 처리 경고의 줄 번호가 원문 줄과 맞게 한다.
            while rows and not rows[0]['text'].strip():
                rows = rows[1:]
            text, warnings = process_tables('\n'.join(row['text'] for row in rows).strip())
            records.append({**envelope, 'type': 'block', 'label': label,
                            'text': text, 'children': []})
            for warning in warnings:
                i = min(max(warning['line'] - 1, 0), len(rows) - 1)
                reviews.append({'source_line': rows[i]['source_line'], 'label': label,
                                'reason': warning['reason']})
    return records, reviews, removed


def collect_text(node):
    return '\n'.join([node['text'], *(collect_text(child) for child in node['children'])]).strip()


def validate_records(records):
    """필수 필드·자료형·계층·중복을 검사하고 오류 문구 목록을 반환한다."""
    errors = []
    if not isinstance(records, list) or not records:
        return ['비어 있지 않은 규정 레코드 목록이 필요합니다.']
    identity = None
    allowed = {'id', 'type', 'label', 'text', 'children', 'parents', 'annotations'}
    ids = set()

    def visit(node, path, parent=None, top=False):
        if not isinstance(node, dict):
            errors.append(f'{path}: 객체 필요')
            return
        top_fields = {'document_id', 'document_name', 'source', 'region', 'region_label'}
        fields = allowed | (top_fields if top else set())
        if set(node) - fields:
            errors.append(f'{path}: 정의되지 않은 필드 {sorted(set(node) - fields)}')
        for key in ('type', 'label', 'text'):
            if not isinstance(node.get(key), str) or (key != 'text' and not node[key].strip()):
                errors.append(f'{path}.{key}: 문자열 필요')
                return
        kind = node['type']
        if not isinstance(node.get('children'), list):
            errors.append(f'{path}.children: 목록 필요')
            return
        if top:
            for key in ('document_id', 'document_name', 'region_label'):
                if not isinstance(node.get(key), str) or not node[key].strip():
                    errors.append(f'{path}.{key}: 비어 있지 않은 문자열 필요')
            region = node.get('region')
            if not isinstance(region, str) or region not in {'body', 'supplement', 'appendix'}:
                errors.append(f'{path}.region: 지원하지 않는 영역')
            if kind != ('article' if region == 'body' else 'block'):
                errors.append(f'{path}.type: 문서 영역과 노드 종류 불일치')
            source = node.get('source')
            if not isinstance(source, dict) or set(source) != {'file', 'effective_date'}:
                errors.append(f'{path}.source: 출처 구조 오류')
            else:
                if not isinstance(source['file'], str) or not source['file'].strip():
                    errors.append(f'{path}.source.file: 파일명 필요')
                if source['effective_date'] is not None and not _is_iso_date(source['effective_date']):
                    errors.append(f'{path}.source.effective_date: 날짜 오류')
        elif kind not in CHILD_TYPES.get(parent, set()):
            errors.append(f'{path}: 허용되지 않은 계층 {parent}/{kind}')
        if kind == 'block' and node['children']:
            errors.append(f'{path}: 부속 블록 children은 빈 목록이어야 합니다.')
        if kind == 'block' and 'id' in node:
            errors.append(f'{path}.id: 부칙·별표에는 조항 ID를 붙이지 않습니다.')
        if kind != 'block':
            if not isinstance(node.get('id'), str) or not node['id'].strip():
                errors.append(f'{path}.id: 조항 ID 필요')
            elif node['id'] in ids:
                errors.append(f'{path}.id: 조항 ID 중복 {node["id"]}')
            else:
                ids.add(node['id'])
        if 'parents' in node:
            parents = node['parents']
            if kind != 'article' or not isinstance(parents, dict):
                errors.append(f'{path}.parents: 조문의 상위 맥락 객체 필요')
            elif (set(parents) - {'part', 'chapter', 'section'}
                  or any(not isinstance(v, str) or not v for v in parents.values())):
                errors.append(f'{path}.parents: 편·장·절 문자열 필요')
        if 'annotations' in node:
            annotations = node['annotations']
            if kind != 'article' or not isinstance(annotations, list):
                errors.append(f'{path}.annotations: 조문 주석 목록 필요')
            else:
                for item in annotations:
                    if (not isinstance(item, dict) or set(item) != {'source_line', 'text'}
                            or type(item.get('source_line')) is not int or item['source_line'] < 1
                            or not isinstance(item.get('text'), str)):
                        errors.append(f'{path}.annotations: 주석 형식 오류')
        seen = set()
        for i, child in enumerate(node['children']):
            if isinstance(child, dict) and all(isinstance(child.get(k), str) for k in ('type', 'label')):
                key = (child.get('type'), child.get('label'))
                if key in seen:
                    errors.append(f'{path}: 자식 번호 중복 {key}')
                seen.add(key)
            visit(child, f'{path}.children[{i}]', kind)

    seen = set()
    for i, record in enumerate(records):
        visit(record, f'records[{i}]', top=True)
        if not isinstance(record, dict):
            continue
        current = (record.get('document_id'), record.get('document_name'), record.get('source'))
        if identity is None:
            identity = current
        elif current != identity:
            errors.append('문서 ID·문서명·출처가 레코드마다 다릅니다.')
        if not all(isinstance(record.get(k), str) for k in ('region', 'region_label', 'label')):
            continue
        key = (record.get('region'), record.get('region_label'), re.sub(r'\(.*', '', record.get('label', '')))
        if key in seen:
            errors.append(f'최상위 레코드 중복: {key}')
        seen.add(key)
    if not any(isinstance(r, dict) and r.get('region') == 'body' for r in records):
        errors.append('본문 조문이 없습니다.')
    return errors


def _validate_source(source, prepared, sections, excluded, records, rules):
    """전처리·영역 분리·조문 구성 과정에서 원문이 빠지거나 바뀌지 않았는지 검사한다."""
    errors = []
    by_line = {}
    for row in prepared['removed'] + prepared['lines']:
        by_line[row['source_line']] = by_line.get(row['source_line'], '') + row['text']
    if '\n'.join(by_line[i] for i in sorted(by_line)) != '\n'.join(source['raw_text'].splitlines()):
        errors.append('전처리 중 원문 누락/변경')
    assigned = ([r for s in sections for r in s['lines']]
                + [{k: v for k, v in r.items() if k != 'reason'} for r in excluded])
    if sorted(assigned, key=lambda r: r['source_line']) != prepared['lines']:
        errors.append('영역 분리 중 원문 누락/중복')
    body_rows = [r for s in sections if s['region'] == 'body' for r in s['lines']]
    body_rows, _ = documents.correct(prepared['metadata']['document_name'], body_rows)   # 보정한 줄 기준으로 대조
    expected = '\n'.join(r['text'] for r in body_rows
                         if not rules.heading(r['text'])
                         and not common.ANNOTATION.fullmatch(r['text'].strip()))
    actual = '\n'.join(collect_text(r) for r in records if r['region'] == 'body')
    if _compact(expected) != _compact(actual):
        errors.append('본문 재구성 불일치: 번호·문구·순서 누락/변경')
    expected_notes = [{'source_line': row['source_line'], 'text': row['text'].strip()}
                      for row in body_rows if common.ANNOTATION.fullmatch(row['text'].strip())]
    actual_notes = [note for record in records for note in record.get('annotations', [])]
    if expected_notes != actual_notes:
        errors.append('조문 주석 누락/변경')
    context, articles = {}, iter(r for r in records if r['region'] == 'body')
    for row in body_rows:
        title = rules.heading(row['text'])
        if title:
            _set_parent(context, *title)
        elif common.article_label(row['text'].strip()):
            article = next(articles, None)
            if article is None or article.get('parents', {}) != context:
                errors.append(f"{row['source_line']}행: 상위 제목 소속 불일치")
    return errors


def run(path):
    """TXT 하나를 레코드 목록으로 변환하고, 검증 결과와 처리 과정(trace)을 함께 반환한다."""
    source = read_source(path)
    rules = select_rules(source['raw_text'])
    prepared = rules.preprocess(source)
    sections, excluded = rules.split_sections(prepared)
    records, reviews, removed = build_records(source, prepared, sections, rules)
    issues = validate_records(records)
    if not issues:
        issues.extend(_validate_source(source, prepared, sections, excluded, records, rules))
    if prepared['metadata']['effective_date'] is None:
        reviews.append({'source_line': 1, 'label': '문서 메타데이터', 'reason': '시행일 정보 없음: null 보존'})
    counts = Counter(r['region'] for r in records)
    validation = {'status': 'failed' if issues else 'review_required' if reviews else 'passed',
                  'issues': issues, 'review_issues': reviews, 'counts': dict(counts)}
    return {'records': records, 'validation': validation,
            'trace': {'source': source, 'excluded': excluded + removed}}


def save_json(result, output_path, *, overwrite=False):
    """검증을 통과한 레코드 목록을 저장한다."""
    if result['validation']['status'] == 'failed':
        raise ValueError(f"검증 실패: {result['validation']['issues'][:5]}")
    issues = validate_records(result['records'])
    if issues:
        raise ValueError(f'레코드 형식 오류: {issues[:5]}')
    write_json(result['records'], output_path, overwrite=overwrite)


def write_json(records, path, *, overwrite=False):
    """JSON을 저장한다. 같은 내용이면 그대로 두고, 다른 내용은 overwrite=True일 때만 교체한다."""
    path = Path(path)
    payload = json.dumps(records, ensure_ascii=False, indent=2) + '\n'
    if path.exists():
        if path.read_text(encoding='utf-8') == payload:
            return
        if not overwrite:
            raise FileExistsError(f'기존 결과와 내용이 다릅니다. 갱신하려면 --overwrite를 지정하십시오: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    # 임시 파일에 다 쓴 뒤 교체하여, 중간에 실패해도 반쯤 쓰인 파일이 남지 않게 한다.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        stream.write(payload)
    try:
        os.chmod(stream.name, 0o644)
        os.replace(stream.name, path)
    finally:
        Path(stream.name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--overwrite', action='store_true', help='기존 결과와 내용이 달라도 덮어쓴다')
    args = parser.parse_args()
    paths = sorted(args.input.glob('*.txt')) if args.input.is_dir() else [args.input]
    if not paths:
        parser.error('입력 TXT가 없습니다.')
    failed = False
    for path in paths:
        try:
            result = run(path)
            output = args.output / (unicodedata.normalize('NFC', path.stem) + '.json')
            save_json(result, output, overwrite=args.overwrite)
            print(json.dumps({'file': path.name, **result['validation']}, ensure_ascii=False))
        except (ValueError, OSError) as error:
            failed = True
            print(json.dumps({'file': path.name, 'status': 'failed', 'error': str(error)},
                             ensure_ascii=False))
    raise SystemExit(int(failed))


if __name__ == '__main__':
    main()
