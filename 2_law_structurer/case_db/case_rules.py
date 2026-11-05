"""심의사례 셀 해석 규칙. 파일 입출력 없이 입력을 받아 해석 결과를 반환한다.

헤더 별칭 → 사례 시작 단서 → 복합 셀 분리 → 날짜·이름·결과 정규화.
해석하지 못한 번호·날짜는 null로 두고 검토 사유를 남긴다.
"""
import copy
from datetime import date
import re
import unicodedata


# 원문 지면을 확인하고 적용하는 PDF별 보정. sha256이 같은 PDF에만 적용한다.
VERIFIED_BOUNDARY_REPAIRS = ({
    'sha256': '3707cd54a236c6708de1308a14953fcf7990cb2c4126c04eb46cbac0defb7231',
    'from_id': '2025-05-0076', 'to_id': '2026-방송-10-0037',
    'page': 2, 'date_text': '2024.07.02.화,', 'time_text': '19:40~21:00',
    # 아래 사례(뉴스데스크) 본문에 같은 날짜가 '(7.2.)'로 적혀 있어 소속을 확인할 수 있다.
    'evidence': r'\(\s*7\.2\.\s*\)',
    'reason': 'PDF 2쪽에서 날짜 글자가 행 경계에 걸쳐 위 사례의 셀로 추출됨',
},)

ALIASES = {
    '순번': 'sequence', '월': 'month',
    '의결일': 'case_date', '의결번호': 'case_id',
    '의결번/심의부문': 'identity',
    '의결일/의결번호/심의부문': 'identity',
    '방송사': 'broadcaster', '채널': 'channel', '채널명': 'channel',
    '방송사/프로그램명': 'party_program',
    '프로그램명': 'program_name', '광고명': 'program_name',
    '프로그램명(방송일시)': 'program_broadcast',
    '광고명(방송일시)': 'program_broadcast', '방송일시': 'broadcast_time',
    '의결내용': 'content', '논의내용': 'content',
    '문제방송내용': 'content', '관련조항': 'provisions',
    '심의결과': 'result', '의결내역': 'result',
    '심의의결내용': 'result',
}

ID = re.compile(r'(?<!\d)(?:제\s*)?(20\d{2})\s*-\s*(?:(방송|광고)\s*-\s*)?'
                r'(\d{1,2})\s*-\s*(\d(?:\s*\d){3})(?!\d)(?:\s*호)?')
DATE = re.compile(r'(?<!\d)(20\d{2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?!\d)\s*\.?')
TIME = re.compile(r'(\d{1,2})\s*:\s*(\d{2})\s*[~～∼\-–]\s*'
                  r'(\d{1,2})\s*:\s*(\d{2})')
EMPTY_PROVISIONS = {'-', '—', '–'}
QUOTES = {"'": "'", '‘': '’', '“': '”', '"': '"'}


def compact(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFC', value or ''))


def normalize_text(value):
    """NFC로 정규화하고 연속 공백을 한 칸으로 줄인다."""
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', value or '')).strip()


def repair_boundaries(bound, source):
    """VERIFIED_BOUNDARY_REPAIRS에 등록된 PDF에만 방송일시 날짜 경계 보정을 적용한다."""
    result = copy.deepcopy(bound)
    result['corrections'] = []
    rows = [r for r in result['rows'] if r['kind'] == 'data']

    def identity(row):
        return case_ids('\n'.join(v or '' for v, h in zip(row['cells'], row['header']['handlers'])
                                  if h in ('identity', 'case_id')))

    for spec in VERIFIED_BOUNDARY_REPAIRS:
        if source['sha256'] != spec['sha256']:
            continue
        matches = [(a, b) for a, b in zip(rows, rows[1:])
                   if a['page'] == b['page'] == spec['page'] and a['table'] == b['table']
                   and identity(a) == [spec['from_id']] and identity(b) == [spec['to_id']]]
        if len(matches) != 1:
            raise ValueError('방송일시 보정 대상 행을 하나로 특정할 수 없습니다.')
        above, below = matches[0]
        ai = above['header']['handlers'].index('broadcast_time')
        bi = below['header']['handlers'].index('broadcast_time')
        a, b = above['cells'][ai], below['cells'][bi]
        if not a.endswith(spec['date_text']) or not b.startswith(spec['time_text']):
            raise ValueError('방송일시 보정 대상 셀의 원시 값이 예상과 다릅니다.')
        content = '\n'.join(v or '' for v, h in zip(below['cells'], below['header']['handlers'])
                            if h == 'content')
        if not re.search(spec['evidence'], content):
            raise ValueError('방송일시 보정의 근거인 본문 날짜를 찾을 수 없습니다.')
        above['original_cells'], below['original_cells'] = list(above['cells']), list(below['cells'])
        above['cells'][ai] = a[:-len(spec['date_text'])].rstrip()
        below['cells'][bi] = spec['date_text'] + '\n' + b
        result['corrections'].append({'from_id': spec['from_id'], 'to_id': spec['to_id'],
                                      'moved_text': spec['date_text']})
    return result


def make_name_references(lines):
    """원시 셀의 각 줄을 이름 정리의 근거로 모은다."""
    return {value for value in (normalize_text(line) for line in lines) if value}


def normalize_name(value, field, name_references=frozenset()):
    """이름 셀의 줄바꿈을 정리하고, 바꾼 내역과 판단이 애매했는지 여부를 반환한다.

    다른 셀에 같은 이름이 한 줄로 적힌 곳이 하나뿐이면 그 표기를 따른다.
    """
    if not value:
        return None, [], False
    value = '\n'.join(normalize_text(line) for line in value.splitlines()).strip()
    original = value
    notes = []
    # '(15\n초)', '(5\n분 30초)'처럼 괄호 안 광고 길이가 나뉜 경우는 숫자와 단위를 붙인다.
    value = re.sub(r'(\(\d+)\n(?=(?:초|분)[\s)])', r'\1', value)
    # 광고 길이 '(15초)' 앞의 줄바꿈은 단어가 나뉜 것이 아니므로 공백으로 바꾼다.
    value = re.sub(r'\n(?=\(\d+\s*(?:초|분)\))', ' ', value)
    if field in ('broadcaster', 'channel'):
        value = re.sub(r'\n(?=-(?:표준)?(?:FM|AM|TV)\b)', '', value)
    if value != original:
        notes.append({'method': 'syntactic_separator', 'before': original, 'after': value})
    if '\n' not in value:
        return value, notes, False
    opening = closing = ''
    core = value
    if len(core) > 1 and core[0] in QUOTES and core[-1] == QUOTES[core[0]]:
        opening, closing, core = core[0], core[-1], core[1:-1]
    # 본문에는 광고명이 길이 표기 없이 나오는 경우가 많다.
    # 길이 표기는 떼어 두고 이름 부분만 근거와 대조한다.
    suffix = ''
    duration = re.search(r'\s*\(\d+\s*(?:초|분)\)$', core) if field == 'program_name' else None
    if duration:
        suffix, core = core[duration.start():], core[:duration.start()]
    # 줄바꿈 자리는 '붙임' 또는 '한 칸 띄움'으로만 바꾸고, 기존 띄어쓰기는 유지한다.
    pattern = re.compile('[ \\t]*'.join(re.escape(line) for line in core.split('\n')))
    found = {match[0] for text in name_references for match in pattern.finditer(text)}
    if len(found) == 1:
        resolved = opening + found.pop() + suffix + closing
        notes.append({'method': 'unique_single_line_source', 'before': value, 'after': resolved})
        return resolved, notes, False
    if len(found) > 1:
        notes.append({'method': 'conflicting_source_spellings', 'candidates': sorted(found)})
        resolved = normalize_text(value)
        notes.append({'method': 'final_name', 'before': value, 'after': resolved})
        return resolved, notes, True

    # 'JTB\nC'는 원시 셀 어딘가에 'JTBC'가 독립된 영문 단어로 있을 때만 붙인다.
    def latin(m):
        token = m[1] + m[2]
        token_pattern = re.compile(r'(?<![A-Za-z])' + re.escape(token) + r'(?![A-Za-z])')
        if not any(token_pattern.search(text) for text in name_references):
            return m[0]
        notes.append({'method': 'observed_latin_token', 'token': token})
        return token

    value = re.sub(r'([A-Za-z]+)\n([A-Za-z]+)', latin, value)
    # 근거가 없으면 한글 사이의 줄바꿈은 단어 중간에서 끊긴 것으로 보고 붙인다.
    # 띄어 쓴 이름이면 그 자리가 원래 띄어쓰기였을 수도 있으므로 애매한 것으로 표시한다.
    joined = re.sub(r'(?<=[가-힣])\n(?=[가-힣])', '', value)
    ambiguous = joined != value and ' ' in joined
    # 나머지 줄바꿈(한글과 영문·숫자 사이 등)은 한 칸 공백으로 바꾼다.
    value = normalize_text(joined)
    if value != original:
        notes.append({'method': 'final_name', 'before': original, 'after': value})
    return value, notes, ambiguous


def identify_header(cells):
    """헤더 행이면 열별 필드 이름(handlers)을 반환하고, 아니면 None을 반환한다."""
    handlers = [ALIASES.get(compact(c)) for c in cells]
    known = set(handlers) - {None}
    if ('content' not in known or 'provisions' not in known
            or not (known.intersection({'identity', 'case_id'}) or
                    {'sequence', 'case_date', 'program_broadcast'} <= known)):
        return None
    return {'original': list(cells), 'handlers': handlers,
            'unknown_columns': [i for i, h in enumerate(handlers) if h is None]}


def case_ids(text):
    return list(dict.fromkeys('-'.join(x for x in (m[1], m[2], m[3].zfill(2), compact(m[4])) if x)
                              for m in ID.finditer(text or '')))


def iso_date(match):
    return date(*(int(match[i]) for i in (1, 2, 3))).isoformat()


def merge_values(fragments, handler):
    """같은 필드의 셀 값을 순서대로 잇는다.

    다음 페이지에 반복된 메타데이터는 한 번만 남기고, 의결 본문은 반복되어도 모두 잇는다.
    """
    values = []
    for f in fragments:
        if f['handler'] != handler or not (f['value'] or '').strip():
            continue
        value = f['value'].strip()
        if handler != 'content' and values and compact(values[-1]) == compact(value):
            continue
        values.append(value)
    return '\n'.join(values)


def split_party_program(text, warnings):
    """'방송사/프로그램명' 복합 셀을 방송사와 프로그램명으로 나눈다."""
    # 따옴표로 시작하는 프로그램명이 있으면 방송사명이 여러 줄이어도 경계가 분명하다.
    quoted = re.search(r"['‘“]", text)
    if quoted and text[:quoted.start()].strip():
        return text[:quoted.start()].strip(), text[quoted.start():].strip()
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if len(lines) < 2:
        warnings.append('방송사/프로그램명 복합 셀 분리 불확실')
        return text or None, None
    # 다음 줄로 넘어간 TV/FM/AM은 방송사명에 속한다.
    cut = 1
    while cut < len(lines) and lines[cut] in ('TV', 'FM', 'AM', '-TV', '-FM', '-AM'):
        cut += 1
    if cut == len(lines):
        warnings.append('방송사/프로그램명에서 프로그램명 없음')
        return '\n'.join(lines), None
    return '\n'.join(lines[:cut]), '\n'.join(lines[cut:])


def time_range(match, warnings):
    """시각 범위 하나를 ('HH:MM', 'HH:MM')로 만든다. 유효하지 않으면 (None, None)이다."""
    hh, mm, eh, em = map(int, match.groups())
    valid = 0 <= hh <= 24 and 0 <= eh <= 24 and mm < 60 and em < 60
    valid = valid and (hh != 24 or mm == 0) and (eh != 24 or em == 0)
    if not valid:
        warnings.append('유효하지 않은 방송 시간: ' + match[0])
        return None, None
    return f'{hh:02}:{mm:02}', f'{eh:02}:{em:02}'


def parse_broadcast_time(text, warnings):
    """방송일시 셀에서 날짜별 시작·종료 시각 목록을 만든다."""
    if not text.strip():
        return []
    dates = list(DATE.finditer(text))
    if not dates:
        warnings.append('방송일시에서 날짜를 해석하지 못함: 원시 셀 확인')
        return []
    output = []
    # 날짜 없이 시각으로 시작하면 PDF 셀 경계에서 날짜가 잘렸을 수 있다.
    # 이 시각은 날짜를 null로 두고 검토 항목으로 남긴다.
    for t in TIME.finditer(text[:dates[0].start()]):
        warnings.append('첫 방송 시간에 대응하는 날짜 없음: 인접 행/셀 경계 검토')
        start, end = time_range(t, warnings)
        output.append({'date': None, 'start_time': start, 'end_time': end})
    for i, m in enumerate(dates):
        end = dates[i + 1].start() if i + 1 < len(dates) else len(text)
        segment = text[m.start():end]
        try:
            day = iso_date(m)
        except ValueError:
            warnings.append('유효하지 않은 방송 날짜: ' + m[0])
            day = None
        times = list(TIME.finditer(segment))
        if not times:
            warnings.append('방송 날짜에 대응하는 시간 범위 없음')
            output.append({'date': day, 'start_time': None, 'end_time': None})
        for t in times:
            start, finish = time_range(t, warnings)
            output.append({'date': day, 'start_time': start, 'end_time': finish})
    return output


def parse_result(text):
    """쉼표와 독립된 결과명으로 구분하고, 줄바꿈된 명칭·금액은 연결한다."""
    values = []
    # '등급\n조정요구'와 '과징금\n(일천만원)'은 각각 한 결과다.
    for chunk in re.split(r'[,，;；]+', text):
        lines = [normalize_text(line) for line in chunk.splitlines() if line.strip()]
        pending = ''
        for line in lines:
            if pending and re.match(r'^(?:권고|의견제시|문제없음|주의|경고|과징금|등급\s*조정)', line):
                values.append(pending)
                pending = line
            else:
                pending = (pending + ' ' + line).strip()
        if pending:
            values.append(pending)
    return list(dict.fromkeys(re.sub(r'등급\s*조정\s*요구', '등급조정 요구', v) for v in values))


def split_cells(group):
    """한 사례의 셀 값을 헤더 의미에 따라 필드별로 나눈다."""
    warnings = list(group['warnings'])

    def get(handler):
        return merge_values(group['fragments'], handler)

    identity = get('identity')
    id_text = get('case_id') or identity
    ids = case_ids(id_text)
    if len(ids) != 1 and not group['generated_id']:
        warnings.append('의결번호가 없거나 여러 개: ' + id_text)
    date_text = get('case_date') or identity
    # 의결번호(예: 2025-05-0076)는 날짜와 형식이 비슷하므로 먼저 지운다.
    date_text = ID.sub('', date_text)
    dm = DATE.search(date_text)
    case_date = None
    if dm:
        try:
            case_date = iso_date(dm)
        except ValueError:
            warnings.append('의결일이 유효한 날짜가 아님')
    if case_date is None:
        warnings.append('의결일을 해석하지 못함')
    category = DATE.sub('', ID.sub('', identity))
    category = category.strip(' \n\t/.,') or None
    broadcaster, program = get('broadcaster') or None, get('program_name') or None
    party = get('party_program')
    if party:
        broadcaster, program = split_party_program(party, warnings)
    broadcast_text = get('broadcast_time')
    combined = get('program_broadcast')
    if combined:
        m = DATE.search(combined)
        if m:
            prefix = combined[:m.start()].rstrip()
            if prefix.endswith('('):
                prefix = prefix[:-1].rstrip()
            program = prefix or None
            broadcast_text = combined[m.start():]
        else:
            program = combined
            warnings.append('프로그램/방송일시 복합 셀의 날짜 분리 실패')
    broadcast_time = parse_broadcast_time(broadcast_text, warnings)
    if not broadcast_time:
        warnings.append('방송일시 목록 비어 있음')
    results = parse_result(get('result'))
    return {'case_id': ids[0] if len(ids) == 1 else group['case_id'],
            'case_date': case_date, 'review_category': category,
            'broadcaster': broadcaster, 'channel': get('channel') or None,
            'program_name': program, 'broadcast_time': broadcast_time,
            'content': get('content'), 'provisions': get('provisions'),
            'result': list(dict.fromkeys(results)), 'warnings': list(dict.fromkeys(warnings))}


def row_identity(row):
    """의결번호 없는 양식에서 새 사례를 나타내는 날짜·프로그램·채널 단서."""
    def get(handler):
        return '\n'.join(v or '' for v, h in zip(row['cells'], row['header']['handlers'])
                         if h == handler)

    day, program = get('case_date'), get('program_broadcast') or get('program_name') or get('party_program')
    if DATE.search(day) and program.strip():
        return tuple(compact(v) for v in (day, program, get('broadcaster'), get('channel')))
    return None


def normalize_parts(parts, name_references=frozenset()):
    """분리된 값의 공백과 이름 줄바꿈을 정리하고, 이름을 바꾼 내역을 함께 반환한다.

    줄바꿈 자리의 띄어쓰기를 확정할 수 없는 이름은 warnings에 검토 사유를 더한다.
    """
    result = copy.deepcopy(parts)
    notes = []
    for field in ['broadcaster', 'channel', 'program_name']:
        value, changes, ambiguous = normalize_name(result[field], field, name_references)
        result[field] = value
        notes.extend({'field': field, **change} for change in changes)
        if ambiguous:
            result['warnings'].append(f'이름 줄바꿈 자리의 띄어쓰기 확인 필요: {value}')
    for field in ['review_category', 'content', 'provisions']:
        result[field] = normalize_text(result[field]) if result[field] else None
    if result['provisions'] in EMPTY_PROVISIONS:
        result['provisions'] = None
    result['name_normalizations'] = notes
    return result
