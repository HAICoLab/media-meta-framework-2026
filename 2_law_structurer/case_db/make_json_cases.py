"""심의의결 현황 PDF를 사례 단위 JSON으로 변환한다.

실행: python make_json_cases.py data output/cases
PDF 표 추출 → 헤더 해석 → 사례 연결 → 값 분리·정규화 → 원시 셀 대조 검증 → 저장.
"""
import argparse
from collections import Counter
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

if __package__:
    from . import case_rules, pdf_processor
else:
    import case_rules
    import pdf_processor

ROOT = Path(__file__).resolve().parent
TIME_FORMAT = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d|24:00')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_source(path, metadata=None):
    """PDF 해시를 계산하고 sources.json의 출처 정보와 합친다."""
    path = Path(path)
    metadata = metadata or {}
    sha = digest(path)
    if metadata.get('sha256') and metadata['sha256'] != sha:
        raise ValueError(f'출처 목록의 해시와 현재 PDF가 다릅니다: {path.name}')
    return {**metadata, 'file': path.name, 'sha256': sha, 'report_period': metadata.get('report_period')}


def cell_lines(raw):
    """모든 표 셀의 줄을 모은다. 이름 줄바꿈을 정리할 때 근거로 쓴다."""
    lines = []
    for page in raw['pages']:
        for table in page['tables']:
            for row in table['rows']:
                for cell in row:
                    lines.extend(line for line in (cell or '').splitlines() if line.strip())
    return lines


def read_inputs(paths, metadata_by_file):
    """입력 PDF를 모두 읽는다.

    이름 근거는 함께 읽은 모든 PDF의 셀에서 모은다. 읽지 못한 파일은 errors에 사유를 남긴다.
    """
    raw_by_file, errors, lines = {}, {}, []
    for path in paths:
        try:
            raw = pdf_processor.extract_tables(path, read_source(path, metadata_by_file.get(path.name)))
        except (OSError, ValueError) as exc:
            errors[path.name] = str(exc)
            continue
        raw_by_file[path.name] = raw
        lines.extend(cell_lines(raw))
    return {'raw_by_file': raw_by_file, 'errors': errors,
            'name_references': case_rules.make_name_references(lines)}


def bind_headers(raw):
    """반복 헤더를 찾고, 인접 페이지의 열 좌표가 맞을 때만 헤더를 상속한다."""
    rows, reviews = [], []
    previous = None
    for page in raw['pages']:
        if not page['tables']:
            reviews.append({'page': page['page'], 'reason': '표 없음'})
            previous = None
        for table in page['tables']:
            header = None
            for ri, cells in enumerate(table['rows'], 1):
                found = case_rules.identify_header(cells)
                ref = {'page': page['page'], 'table': table['table'], 'row': ri}
                if found:
                    if found['unknown_columns']:
                        reviews.append({**ref, 'reason': '미지원 헤더 열', 'headers': found['original']})
                    if header and found['handlers'] != header['handlers']:
                        reviews.append({**ref, 'reason': '한 표 안에서 헤더 변경'})
                    header = found
                    rows.append({**ref, 'kind': 'header', 'header': header, 'cells': cells})
                    continue
                if not any((c or '').strip() for c in cells):
                    rows.append({**ref, 'kind': 'empty', 'cells': cells})
                    continue
                # '심의의결 현황' 같은 제목 행
                filled = [c for c in cells if c and c.strip()]
                if len(filled) == 1 and '심의의결' in case_rules.compact(filled[0]):
                    rows.append({**ref, 'kind': 'title', 'cells': cells})
                    continue
                if header is None and previous and previous['page'] == page['page'] - 1:
                    candidate = previous['header']
                    if len(candidate['handlers']) == len(cells):
                        # 열 수만으로는 부족하므로 페이지 폭 대비 열 경계 위치까지 비교한다.
                        bounds = table['cell_bboxes'][ri - 1]
                        current = [(b[0] / page['width'], b[2] / page['width']) if b else None
                                   for b in bounds]
                        if all(a is None or b is None or max(abs(a[0] - b[0]), abs(a[1] - b[1])) < .035
                               for a, b in zip(current, previous['bounds'])):
                            header = candidate
                if header is None or len(header['handlers']) != len(cells):
                    reviews.append({**ref, 'reason': '헤더 없는/열 수 불일치 행: 원시 보존', 'cells': cells})
                    rows.append({**ref, 'kind': 'unmapped', 'cells': cells})
                    continue
                rows.append({**ref, 'kind': 'data', 'header': header, 'cells': cells})
            if header:
                valid = next((b for b in reversed(table['cell_bboxes'])
                              if len(b) == len(header['handlers']) and all(b)), None)
                if valid:
                    previous = {'page': page['page'], 'header': header,
                                'bounds': [(b[0] / page['width'], b[2] / page['width']) for b in valid]}
            else:
                previous = None
    return {'rows': rows, 'reviews': reviews}


def connect_pages(bound, source):
    """의결번호 또는 새 사례의 식별 단서로 원시 행을 사례별로 묶는다."""
    groups, rejected = [], []
    active = None
    for row in bound['rows']:
        if row['kind'] == 'unmapped':
            active = None
        if row['kind'] != 'data':
            continue
        handlers = row['header']['handlers']
        has_id_column = any(h in ('identity', 'case_id') for h in handlers)
        identity = '\n'.join(v or '' for v, h in zip(row['cells'], handlers)
                             if h in ('identity', 'case_id'))
        ids = case_rules.case_ids(identity)
        generated = False
        signature = None
        case_id = None
        if has_id_column:
            if len(ids) > 1 or (identity.strip() and not ids):
                rejected.append({**row, 'reason': '의결번호 셀 해석 실패/여러 번호'})
                active = None
                continue
            if ids:
                case_id = ids[0]
        else:
            signature = case_rules.row_identity(row)
            if signature:
                # 같은 식별 정보가 다음 페이지에 반복된 경우에만 이어 붙인다.
                repeat = (active and active['signature'] == signature
                          and row['page'] == active['rows'][-1]['page'] + 1)
                if not repeat:
                    case_id = f"local:{source['sha256']}:p{row['page']}t{row['table']}r{row['row']}"
                    generated = True
        if case_id:
            if active is None or active['case_id'] != case_id:
                active = {'case_id': case_id, 'rows': [], 'fragments': [], 'warnings': [],
                          'generated_id': generated, 'signature': signature}
                groups.append(active)
            elif active['rows'][-1]['page'] == row['page']:
                active['warnings'].append('동일 페이지에 같은 의결번호 반복: 원문 확인')
        elif active is None or row['page'] > active['rows'][-1]['page'] + 1:
            rejected.append({**row, 'reason': '소속 사례 없는 이어진 행'})
            active = None
            continue
        ref = {k: row[k] for k in ('page', 'table', 'row')}
        active['rows'].append(ref)
        for ci, (value, handler) in enumerate(zip(row['cells'], handlers), 1):
            active['fragments'].append({'handler': handler, 'header': row['header']['original'][ci - 1],
                                       'value': value, 'column': ci, **ref})
    return {'groups': groups, 'rejected': rejected}


def build_case(parts, group, source):
    """정규화한 값으로 최종 사례 JSON 한 건을 구성한다."""
    return {
        'case_id': parts['case_id'], 'case_date': parts['case_date'],
        'case_metadata': {
            'review_category': parts['review_category'], 'broadcaster': parts['broadcaster'],
            'channel': parts['channel'], 'program_name': parts['program_name'],
            'broadcast_time': [{k: b[k] for k in ('date', 'start_time', 'end_time')}
                               for b in parts['broadcast_time']],
        },
        'case_decision': {'content': parts['content'], 'provisions': parts['provisions'],
                          'result': parts['result']},
        'source': {'file': source['file'], 'report_period': source.get('report_period'),
                   'category': source.get('category'), 'page': sorted({r['page'] for r in group['rows']}),
                   'sha256': source['sha256']},
    }


def validate_cases(cases):
    """필수 key·자료형·날짜·시각 형식을 검사하고 오류 목록을 반환한다."""
    errors = []

    def fail(path, reason):
        errors.append({'path': path, 'reason': reason})

    def obj(value, keys, path):
        if not isinstance(value, dict):
            fail(path, '객체 필요')
            return False
        if set(value) != set(keys):
            fail(path, '필수 key 누락 또는 정의되지 않은 key')
        return all(k in value for k in keys)

    def text(value, path, nullable=False):
        if value is None and nullable:
            return
        if not isinstance(value, str) or not value.strip():
            fail(path, '비어 있지 않은 문자열 필요')

    def day(value, path):
        if value is None:
            return
        try:
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError()
            date.fromisoformat(value)
        except ValueError:
            fail(path, '유효한 YYYY-MM-DD 날짜 필요')

    if not isinstance(cases, list) or not cases:
        return [{'path': '$', 'reason': '비어 있지 않은 사례 목록 필요'}]
    for i, c in enumerate(cases):
        path = f'$[{i}]'
        if not obj(c, ['case_id', 'case_date', 'case_metadata', 'case_decision', 'source'], path):
            continue
        text(c['case_id'], path + '.case_id')
        day(c['case_date'], path + '.case_date')
        meta = c['case_metadata']
        decision = c['case_decision']
        source = c['source']
        meta_keys = ['review_category', 'broadcaster', 'channel', 'program_name', 'broadcast_time']
        if obj(meta, meta_keys, path + '.case_metadata'):
            for k in ['review_category', 'broadcaster', 'channel', 'program_name']:
                text(meta[k], path + '.case_metadata.' + k, True)
            broadcasts = meta['broadcast_time']
            if not isinstance(broadcasts, list):
                fail(path + '.case_metadata.broadcast_time', '목록 필요')
            else:
                for bi, b in enumerate(broadcasts):
                    bp = f'{path}.case_metadata.broadcast_time[{bi}]'
                    if not obj(b, ['date', 'start_time', 'end_time'], bp):
                        continue
                    day(b['date'], bp + '.date')
                    for k in ['start_time', 'end_time']:
                        t = b[k]
                        if t is not None and (not isinstance(t, str) or not TIME_FORMAT.fullmatch(t)):
                            fail(bp + '.' + k, '유효한 HH:MM 시각 필요 (24:00 허용)')
        if obj(decision, ['content', 'provisions', 'result'], path + '.case_decision'):
            text(decision['content'], path + '.case_decision.content')
            text(decision['provisions'], path + '.case_decision.provisions', True)
            if not isinstance(decision['result'], list):
                fail(path + '.case_decision.result', '목록 필요')
            else:
                for v in decision['result']:
                    text(v, path + '.case_decision.result')
        if obj(source, ['file', 'report_period', 'category', 'page', 'sha256'], path + '.source'):
            for k in ['file', 'sha256']:
                text(source[k], path + '.source.' + k)
            for k in ['report_period', 'category']:
                text(source[k], path + '.source.' + k, True)
            if not isinstance(source['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', source['sha256']):
                fail(path + '.source.sha256', 'SHA-256 형식 오류')
            pages = source['page']
            if (not isinstance(pages, list) or not pages
                    or any(type(p) is not int or p < 1 for p in pages)):
                fail(path + '.source.page', '양의 정수 페이지 목록 필요')
            elif len(pages) != len(set(pages)):
                fail(path + '.source.page', '페이지 중복')
    return errors


def validate_output(cases):
    """사례 목록 파일의 형식, 사례 ID 중복, 사례 간 출처 일치를 검사한다."""
    errors = validate_cases(cases)
    if errors:
        return errors
    ids = [c['case_id'] for c in cases]
    if len(ids) != len(set(ids)):
        errors.append({'path': '$', 'reason': '사례 ID 중복'})
    source = cases[0]['source']
    for i, c in enumerate(cases):
        for key in ['file', 'sha256', 'report_period', 'category']:
            if c['source'][key] != source[key]:
                errors.append({'path': f'$[{i}].source.{key}', 'reason': '한 PDF의 사례끼리 출처 불일치'})
    return errors


def validate_against_raw(trace, cases):
    """원시 행·의결번호·본문·관련조항·이름이 빠지거나 바뀌지 않았는지 검사한다."""
    raw, bound, joined = trace['raw'], trace['bound'], trace['joined']
    errors, reviews = [], list(bound['reviews'])
    # 방송일시 경계 보정은 기존 글자를 인접 셀로 옮기기만 한다.
    # 방송일시 열 전체에서 글자가 추가·삭제되거나 순서가 바뀌지 않았는지 확인한다.
    before, after = [], []
    for r in bound['rows']:
        if r['kind'] != 'data':
            continue
        for ci, h in enumerate(r['header']['handlers']):
            if h == 'broadcast_time':
                before.append((r.get('original_cells') or r['cells'])[ci] or '')
                after.append(r['cells'][ci] or '')
    if case_rules.compact(''.join(before)) != case_rules.compact(''.join(after)):
        errors.append({'reason': '방송일시 경계 보정에서 원문 문자 누락/추가/순서 변경'})
    errors.extend({k: v for k, v in r.items() if k in ('page', 'table', 'row', 'reason')}
                  for r in joined['rejected'])
    data_refs = [(r['page'], r['table'], r['row']) for r in bound['rows'] if r['kind'] == 'data']
    assigned = [(r['page'], r['table'], r['row']) for g in joined['groups'] for r in g['rows']]
    if Counter(data_refs) != Counter(assigned):
        errors.append({'reason': '사례 연결 과정의 행 누락/중복'})
    if any(r['kind'] == 'unmapped' for r in bound['rows']):
        errors.append({'reason': '공통 필드에 연결되지 않은 원시 행 있음'})
    if not cases:
        errors.append({'reason': '추출 사례 없음'})
    ids = [c['case_id'] for c in cases]
    if len(ids) != len(set(ids)):
        errors.append({'reason': '최종 의결번호 중복'})
    # 의결번호는 표 셀과 별도로, 해당 열 영역만 읽은 텍스트(column_texts)에서도 찾아 대조한다.
    raw_tables = {(p['page'], t['table']): t for p in raw['pages'] for t in p['tables']}
    identity_columns = {(r['page'], r['table'], ci)
                        for r in bound['rows'] if r['kind'] in ('header', 'data')
                        for ci, h in enumerate(r['header']['handlers']) if h in ('identity', 'case_id')}
    text_ids = {did for pn, ti, ci in identity_columns
                for did in case_rules.case_ids(raw_tables[pn, ti]['column_texts'][ci])}
    outside = sorted(text_ids - set(ids))
    if outside:
        reviews.append({'reason': '의결번호 열의 별도 텍스트 추출에 있으나 사례로 연결되지 않은 번호',
                        'case_ids': outside})
    unconfirmed = sorted({g['case_id'] for g in joined['groups'] if not g['generated_id']} - text_ids)
    if unconfirmed:
        reviews.append({'reason': '셀에서 읽은 번호를 별도 열 텍스트 검사로 재확인하지 못함',
                        'case_ids': unconfirmed})
    for case, group, parts, normalized in zip(cases, joined['groups'], trace['split'], trace['normalized']):
        did = case['case_id']
        meta, decision = case['case_metadata'], case['case_decision']
        if did != group['case_id']:
            errors.append({'case_id': did, 'reason': '의결번호 변환 불일치'})
        # 본문·관련조항의 정규화는 공백만 바꾸므로, 원시 셀을 이은 값과 공백 외 글자가 같아야 한다.
        for field in ('content', 'provisions'):
            expected = case_rules.compact(case_rules.merge_values(group['fragments'], field))
            if field == 'provisions' and expected in case_rules.EMPTY_PROVISIONS and decision[field] is None:
                expected = ''
            if expected != case_rules.compact(decision[field]):
                errors.append({'case_id': did, 'reason': field + ' 원시 셀 대비 누락/변경'})
        # 이름 정리도 줄바꿈 자리의 공백만 바꾼다.
        for field in ['program_name', 'broadcaster', 'channel']:
            if case_rules.compact(meta[field]) != case_rules.compact(parts[field]):
                errors.append({'case_id': did, 'reason': field + ' 이름 정리 중 비공백 문자 변경'})
        issues = list(normalized['warnings'])
        if not meta['program_name']:
            issues.append('프로그램/광고명 비어 있음')
        if not decision['provisions'] and decision['result'] != ['문제없음']:
            issues.append('관련조항 없음: 원문 확인 (문제없음 등 가능)')
        if not decision['result']:
            issues.append('심의결과 비어 있음')
        if issues:
            reviews.append({'case_id': did, 'pages': case['source']['page'],
                            'reasons': list(dict.fromkeys(issues))})
    return {'status': 'failed' if errors else 'review_required' if reviews else 'passed',
            'errors': errors, 'review_issues': reviews,
            'counts': {'pages': len(raw['pages']), 'cases': len(cases),
                       'raw_data_rows': len(data_refs), 'assigned_rows': len(assigned),
                       'multi_page_cases': sum(len(c['source']['page']) > 1 for c in cases),
                       'unknown_rows': sum(r['kind'] == 'unmapped' for r in bound['rows']),
                       'identity_column_ids': len(text_ids), 'identity_column_ids_not_grouped': len(outside),
                       'case_ids_not_confirmed_in_column_text': len(unconfirmed),
                       'boundary_repairs': len(bound['corrections']),
                       'name_fields_normalized': sum(
                           len({n['field'] for n in norm['name_normalizations']
                                if n['method'] != 'conflicting_source_spellings'})
                           for norm in trace['normalized']),
                       'duplicate_chars_removed': sum(p['duplicate_chars_removed'] for p in raw['pages'])}}


def validate_extraction(trace, cases):
    """사례의 필드 형식을 검사한 뒤 원시 셀과 대조한다."""
    structural = validate_cases(cases)
    if structural:
        return {'status': 'failed', 'errors': structural, 'review_issues': [], 'counts': {}}
    return validate_against_raw(trace, cases)


def run(path, metadata=None, *, context=None):
    """PDF 하나를 사례 목록으로 변환하고, 검증 결과와 처리 과정(trace)을 함께 반환한다.

    context는 read_inputs()의 결과다. 주어지면 미리 읽은 표와 여러 PDF에서 모은 이름 근거를 쓴다.
    """
    context = context or {}
    raw = context.get('raw_by_file', {}).get(Path(path).name)
    if raw is None:
        raw = pdf_processor.extract_tables(path, read_source(path, metadata))
    source = raw['source']
    references = context.get('name_references')
    if references is None:
        references = case_rules.make_name_references(cell_lines(raw))
    bound = case_rules.repair_boundaries(bind_headers(raw), source)
    joined = connect_pages(bound, source)
    split = [case_rules.split_cells(g) for g in joined['groups']]
    normalized = [case_rules.normalize_parts(p, references) for p in split]
    cases = [build_case(p, g, source) for g, p in zip(joined['groups'], normalized)]
    trace = {'raw': raw, 'bound': bound, 'joined': joined, 'split': split, 'normalized': normalized}
    return {'source': source, 'cases': cases, 'validation': validate_extraction(trace, cases),
            'trace': trace}


def write_json(payload, path, overwrite=False):
    """JSON을 저장한다. 같은 내용이면 그대로 두고, 다른 내용은 overwrite=True일 때만 교체한다."""
    path = Path(path)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + '\n'
    if path.exists():
        if path.read_text(encoding='utf-8') == text:
            return
        if not overwrite:
            raise FileExistsError(f'기존 결과와 내용이 다릅니다. 갱신하려면 --overwrite를 지정하십시오: {path}')
    path.parent.mkdir(parents=True, exist_ok=True)
    # 임시 파일에 다 쓴 뒤 교체하여, 중간에 실패해도 반쯤 쓰인 파일이 남지 않게 한다.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.case-', delete=False) as f:
        f.write(text)
    try:
        os.chmod(f.name, 0o644)
        os.replace(f.name, path)
    finally:
        Path(f.name).unlink(missing_ok=True)


def save_json(result, output_path, *, overwrite=False):
    """검증을 통과한 사례 목록을 저장한다."""
    if result['validation']['status'] == 'failed':
        raise ValueError('검증에 실패한 결과는 저장하지 않습니다.')
    errors = validate_output(result['cases'])
    if errors:
        raise ValueError(f'사례 목록 형식 오류: {errors[:5]}')
    write_json(result['cases'], output_path, overwrite)


def show_flow(result, case_id=None):
    """사례 한 건의 원시 셀·행 연결·값 분리·최종 결과를 출력한다. 출력했으면 True를 반환한다.

    case_id를 생략하면 첫 사례를 출력한다.
    """
    ids = [c['case_id'] for c in result['cases']]
    if not ids or (case_id and case_id not in ids):
        return False
    index = ids.index(case_id) if case_id else 0
    group = result['trace']['joined']['groups'][index]
    print(json.dumps({'source': result['source'], 'cells': group['fragments'], 'rows': group['rows'],
                      'split': result['trace']['split'][index], 'case': result['cases'][index],
                      'validation': result['validation']}, ensure_ascii=False, indent=2))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', nargs='?', type=Path, default=ROOT / 'data')
    parser.add_argument('output', nargs='?', type=Path, default=ROOT / 'output/cases')
    parser.add_argument('--sources', type=Path, default=ROOT / 'data/sources.json')
    parser.add_argument('--overwrite', action='store_true', help='기존 결과와 내용이 달라도 덮어쓴다')
    parser.add_argument('--show-flow', action='store_true', help='사례 한 건의 처리 과정을 출력한다')
    parser.add_argument('--case-id', help='처리 과정을 출력할 사례의 의결번호 (생략하면 첫 사례)')
    args = parser.parse_args()
    show = args.show_flow or args.case_id is not None
    manifest = (json.loads(args.sources.read_text(encoding='utf-8')) if args.sources.exists()
                else {'sources': []})
    metadata = {s['file']: s for s in manifest['sources']}
    paths = sorted(args.input.glob('*.pdf')) if args.input.is_dir() else [args.input]
    if not paths:
        parser.error('입력 PDF가 없습니다.')
    inputs = read_inputs(paths, metadata)
    failed = False
    for path in paths:
        try:
            if path.name in inputs['errors']:
                raise ValueError(inputs['errors'][path.name])
            result = run(path, context=inputs)
            if show and show_flow(result, args.case_id):
                show = False
            if result['validation']['status'] != 'failed':
                save_json(result, args.output / (path.stem + '.json'), overwrite=args.overwrite)
            else:
                failed = True
            print(json.dumps({'file': path.name, **result['validation']}, ensure_ascii=False))
        except (OSError, ValueError) as exc:
            failed = True
            print(json.dumps({'file': path.name, 'status': 'failed', 'error': str(exc)}, ensure_ascii=False))
    if show and args.case_id:
        failed = True
        print(json.dumps({'status': 'failed', 'error': f'사례를 찾을 수 없습니다: --case-id {args.case_id}'},
                         ensure_ascii=False))
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    main()
