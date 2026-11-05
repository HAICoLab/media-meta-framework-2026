"""TXT 표를 읽고 원문 내용이 보존되는 Markdown 문자열을 구성한다."""
import re
from html import escape


def _border(value):
    return bool(value) and all(c.isspace() or '\u2500' <= c <= '\u257f' for c in value)


def _open_top(value):
    # 「1. 주제━━┳━━」처럼 제목이 같은 줄에 붙어 있어도 선 부분만 인식한다.
    match = re.search(r"[─━═]+(?:[┬┳┯┰┱┲╤╥╦][─━═]+)+$", value)
    return match if match and not re.search(r'[\u2500-\u257f]', value[:match.start()]) else None


def _table_rows(lines, width=None, initial_groups=None, boundaries=None):
    """괘선 표의 행을 셀 목록으로 읽는다. 반환값은 (행 목록, 오류 문구)다.

    부분 구분선으로 확인되는 병합만 따르고, 셀 소속이 불확실하거나 행 경계가 불명확하면
    오류 문구를 반환한다. 이 경우 호출한 쪽은 표를 원문 그대로 보존한다.
    """
    rows, pending, groups, separators = [], None, initial_groups, 0
    row_seen = False

    def cell():
        return {"text": "", "unknown": False}

    def occupied(row):
        return row is not None and any(c["text"] or c["unknown"] for c in row)

    def flush():
        nonlocal pending, row_seen
        if pending is not None and row_seen:
            rows.append(pending)
        pending = None
        row_seen = False

    for raw in lines:
        value = raw.strip()
        if not value:
            continue
        if re.search('[─━═]{2,}', value):
            # 경계 문자 사이가 가로선이면 셀을 닫고, 공백이면 다음 행으로 이어간다.
            marks = [m for m in re.finditer(r'[\u2500-\u257f]', value) if m.group() not in '─━═']
            spans = [value[a.end():b.start()] for a, b in zip(marks, marks[1:])]
            if width is None:
                return None, "병합 셀/부분 구분선 범위 불명확"
            if (boundaries and len(value) == boundaries[-1] + 1
                    and all(m.start() in boundaries for m in marks)):
                positions = [m.start() for m in marks]
                spans_groups = [list(range(boundaries.index(a), boundaries.index(b)))
                                for a, b in zip(positions, positions[1:])]
            elif len(spans) == width:
                spans_groups = [[i] for i in range(width)]
            elif groups and len(spans) == len(groups):
                spans_groups = groups
            else:
                return None, "병합 셀/부분 구분선 범위 불명확"
            closed_spans = [bool(re.fullmatch('[─━═]+', x)) for x in spans]
            if not any(closed_spans) or any(re.search('[─━═]', x) and not closed
                                            for x, closed in zip(spans, closed_spans)):
                return None, "병합 셀/부분 구분선 범위 불명확"
            mask, joints = [False] * width, ['┴'] * (width - 1)
            for n, (group, content, closed) in enumerate(zip(spans_groups, spans, closed_spans)):
                for col in group:
                    mask[col] = closed
                if n < len(spans_groups) - 1:
                    joints[group[-1]] = marks[n + 1].group()
                # 일부 셀의 구분선과 다른 셀의 문장이 같은 물리 행에 있을 수 있다.
                if not closed and content.strip() and pending is not None:
                    target = pending[group[0]]
                    if not target["unknown"]:
                        target["text"] += (' ' if target["text"] else '') + content.strip()
                        row_seen = True
            previous = pending
            flush()
            # 구분선으로 닫히지 않은 열은 위 행의 셀 객체를 그대로 이어 써서 세로 병합을 표현한다.
            pending = [cell() if closed or previous is None else previous[i]
                       for i, closed in enumerate(mask)]
            separators += 1
            # ┴는 위쪽 세로선이 끝나고 아래 행에서는 가로 병합됨을 나타낸다.
            down = []
            for i, joint in enumerate(joints):
                if joint in '┴┻┷┸┹┺╧╨╩':
                    down.append(False)
                elif joint in '│┃║':
                    down.append(any(i == g[-1] for g in (groups or [[j] for j in range(width)])))
                else:
                    down.append(True)
            groups, current = [], [0]
            for i, boundary in enumerate(down):
                if boundary:
                    groups.append(current)
                    current = []
                current.append(i + 1)
            groups.append(current)
            for group in groups:
                if len(group) > 1:
                    shared = pending[group[0]]
                    if any((pending[i]["text"] or pending[i]["unknown"]) and pending[i] is not shared
                           for i in group):
                        # 세로·가로 병합이 겹쳐 셀 소속이 불확실하면 미확정(unknown) 셀로 표시한다.
                        shared = {"text": "", "unknown": True}
                    for i in group:
                        pending[i] = shared
            continue
        if not (value.startswith(('┃', '│', '║')) and value.endswith(('┃', '│', '║'))):
            return None, "셀 경계 누락"
        cells = [c.strip() for c in re.split('[┃│║]', value[1:-1])]
        row_seen = True
        if width is None:
            width = len(cells)
            if width < 2:
                return None, "단일열 설명틀 또는 중첩 표"
        if groups is None:
            groups = [[i] for i in range(width)]
        if pending is None:
            pending = [cell() for _ in range(width)]
            for group in groups:
                shared = pending[group[0]]
                for col in group:
                    pending[col] = shared
        if len(cells) != len(groups):
            # 내부 세로선이 빠진 빈 여백 행은 건너뛴다.
            if not any(cells):
                continue
            if not separators or occupied(pending):
                return None, "행 경계/셀 소속 불명확"
            # 행과 열 수는 확정했지만 사라진 구분자의 위치를 모르는 경우다.
            for c in pending:
                c["unknown"] = True
            continue
        number = r"[+-]?\d[\d,]*(?:\.\d+)?\s*%?"
        for group, new in zip(groups, cells):
            target = pending[group[0]]
            if re.fullmatch(number, target["text"]) and re.fullmatch(number, new):
                if any(target is previous for row in rows for previous in row):
                    # 행 경계는 있지만 이어지는 병합 셀의 값이 충돌한다.
                    target["unknown"] = True
                    target["text"] = ""
                else:
                    return None, "병합 셀/행 구분선 없이 여러 수치 행이 이어짐"
        for group, content in zip(groups, cells):
            target = pending[group[0]]
            if content and not target["unknown"]:
                target["text"] += (' ' if target["text"] else '') + content
    flush()
    if not rows:
        return None, "행 경계 불명확"
    # GFM에는 셀 병합이 없으므로 병합 내용은 왼쪽 위 셀에 한 번만 쓴다.
    seen, result = set(), []
    for row in rows:
        values = []
        for c in row:
            if c['unknown']:
                values.append(None)
            elif id(c) in seen:
                values.append('')
            else:
                values.append(c['text'])
            seen.add(id(c))
        result.append(values)
    unknown = sum(c is None for row in result for c in row)
    return result, f"셀 소속 불확실: {unknown}개 셀" if unknown else None


def split_repeated_numeric_rows(lines):
    """가로선 없이 반복되는 '명칭 + 백분율' 행에만 행 경계를 보완한다."""
    result, block = [], []

    def flush():
        if not block:
            return
        cells = [[s.strip() for s in re.split('[┃│║]', line.strip()[1:-1])]
                 for line in block]
        widths = {len(row) for row in cells}
        if len(widths) != 1:
            result.extend(block)
            block.clear()
            return
        width = len(cells[0])
        percent = re.compile(r'[+-]?\d+(?:\.\d+)?\s*%')
        repeated = [i for i in range(width) if sum(bool(percent.fullmatch(row[i])) for row in cells) > 1]
        if not repeated or repeated[0] == 0:
            result.extend(block)
            block.clear()
            return
        anchor = repeated[0] - 1
        starts = [i for i, row in enumerate(cells)
                  if row[anchor] and any(percent.fullmatch(row[j]) for j in repeated)]
        if len(starts) < 2 or any(any(c) for c in cells[:starts[0]]):
            result.extend(block)
            block.clear()
            return
        records = []
        group_titles = [' '.join(row[j] for row in cells if row[j]) for j in range(anchor)]
        for number, start in enumerate(starts):
            end = starts[number + 1] if number + 1 < len(starts) else len(cells)
            record = [' '.join(row[j] for row in cells[start:end] if row[j]) for j in range(width)]
            # 앞 열의 묶음 제목은 첫 행에만 쓴다. 원문에 적용 범위가 없으므로 아래 행은 빈칸으로 둔다.
            record[:anchor] = group_titles if number == 0 else [''] * anchor
            records.append(record)
        for number, record in enumerate(records):
            if number:
                result.append('├' + '┼'.join(['──'] * width) + '┤')
            result.append('│' + '│'.join(record) + '│')
        block.clear()

    for line in lines:
        value = line.strip()
        if (value.startswith(('│', '┃', '║')) and value.endswith(('│', '┃', '║'))
                and not re.search('[─━═]{2,}', value)):
            block.append(line)
        else:
            flush()
            result.append(line)
    flush()
    return result


def extract_rows(lines, top, bottom):
    """가로선의 교차 위치로 열을 확정하고, 병합된 머리행도 같은 열에 맞춘다."""
    lines = split_repeated_numeric_rows(lines)
    width, groups = None, None
    # 왼쪽 병합 셀의 공백만 축약된 부분 구분선은 오른쪽 끝을 기준으로 복원한다.
    restored = []
    for line in lines:
        value = line.strip()
        if (re.fullmatch(r'[│┃║][^\u2500-\u257f]*[├┠┣][─━═┬┳┯┰┱┲┼┿╂╋┴┻┷┸┹┺]+[│┃║]', value)
                and len(value) < len(top)):
            first = next(i for i, char in enumerate(value[1:], 1) if char in '├┠┣')
            value = value[:first] + ' ' * (len(top) - len(value)) + value[first:]
        restored.append(value)
    lines = restored
    full = [line.strip() for line in [top, *lines, bottom]
            if _border(line.strip()) and len(line.strip()) == len(top)]
    boundaries = sorted({i for line in full for i, char in enumerate(line)
                         if char not in '─━═' and not char.isspace()})
    if len(boundaries) >= 2:
        width = len(boundaries) - 1
        positions = [i for i, char in enumerate(top) if char not in '─━═']
        groups = [list(range(boundaries.index(a), boundaries.index(b)))
                  for a, b in zip(positions, positions[1:])]
    return _table_rows(lines, width, groups, boundaries)


# T자 교차선의 위/아래 연결을 구분한다. 전체 가로선에서도 양쪽 열 구성이 다를 수 있다.
_DOWN_JOINTS = '┬┳┯┰┱┲╤╥╦┼╂┿╋╪╫╬'
_UP_JOINTS = '┴┻┷┸┹┺╧╨╩┼╂┿╋╪╫╬'


def _full_grid_boundary(value, width):
    """모든 셀을 닫는 완전한 가로선인지 확인한다. 공백·문장·이어지는 세로선이 섞이면 False다."""
    return (len(value) == width and bool(re.fullmatch(
        r'[┌┏╔├┠┣└┗╚][─━═]+(?:[' + _DOWN_JOINTS + _UP_JOINTS +
        r'][─━═]+)*[┐┓╗┤┨┫┘┛╝]', value)))


def _project_grid_boundary(value, top):
    """구분선에서 해당 구간으로 이어지는 세로선만 남겨 실제 열 경계를 만든다."""
    joints = _DOWN_JOINTS if top else _UP_JOINTS
    middle = ''.join(('┬' if top else '┴') if char in joints else '─' for char in value[1:-1])
    return ('┌' if top else '└') + middle + ('┐' if top else '┘')


def split_grid_sections(lines, top, bottom):
    """전체 가로선과 일정한 열 경계로 증명되는 구간만 나눈다. 다단 머리행 등은 보존한다."""
    values = [top, *(line.strip() for line in lines), bottom]
    boundaries = [i for i, value in enumerate(values) if _full_grid_boundary(value, len(top))]
    if not boundaries or boundaries[0] != 0 or boundaries[-1] != len(values) - 1:
        return None
    sections = []
    for begin, end in zip(boundaries, boundaries[1:]):
        if begin + 1 == end:
            return None
        section_top = _project_grid_boundary(values[begin], True)
        section_bottom = _project_grid_boundary(values[end], False)
        upper = tuple(i for i, char in enumerate(section_top) if char == '┬')
        lower = tuple(i for i, char in enumerate(section_bottom) if char == '┴')
        # 위·아래 경계의 열 위치가 다르면 사분기/월 같은 다단 머리행이므로 None을 반환해 기존 표를 유지한다.
        if upper != lower:
            return None
        if sections and sections[-1]['columns'] == upper:
            sections[-1]['lines'].extend([values[begin], *values[begin + 1:end]])
            sections[-1]['bottom'] = section_bottom
        else:
            sections.append({'top': section_top, 'bottom': section_bottom,
                             'lines': values[begin + 1:end], 'columns': upper})
    return sections if len(sections) > 1 else None


def extract_grid_sections(lines, top, bottom, original_rows):
    """분리 전후 셀 내용과 행 수가 같은 경우에만 작은 표를 채택한다."""
    sections = split_grid_sections(lines, top, bottom)
    if not sections:
        return None
    grids = []
    for section in sections:
        rows, error = extract_rows(section['lines'], section['top'], section['bottom'])
        if rows is None or error or not _content_preserved(section['lines'], rows):
            return None
        grids.append(rows)

    def signature(rows):
        return [[_compact(cell) for cell in row if cell.strip()] for row in rows]

    combined = [row for grid in grids for row in grid]
    # 원래의 빈 행까지 순서대로 대조하여 문장 이동·누락·중복을 거부한다.
    if signature(combined) != signature(original_rows):
        return None
    # 표마다 생기는 빈 머리행까지 세어, 분리가 오히려 빈칸을 늘리면 기존 표를 유지한다.
    before = sum(map(len, original_rows)) + len(original_rows[0])
    after = sum(sum(map(len, grid)) + len(grid[0]) for grid in grids)
    if after >= before:
        return None
    return grids


def normalize_cell_breaks(value):
    """셀 값 하나의 줄바꿈을 LF로 통일하고 앞뒤 공백을 제거한다."""
    return value.replace('\r\n', '\n').replace('\r', '\n').strip()


def escape_markdown_chars(value):
    """내용 속 파이프·Markdown 기호가 열 경계나 서식으로 해석되는 것을 막는다."""
    value = escape(value, quote=False)
    value = re.sub(r'([\\|`*_\[\]])', r'\\\1', value)
    return value.replace('\n', '<br>')


def render_markdown(rows, *, use_header=None):
    """행 목록을 GFM Markdown 표로 만든다. 첫 행이 열 제목이 아니면 빈 머리행을 넣는다."""
    if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError('표의 행별 열 수가 일치하지 않습니다.')
    if any(not isinstance(cell, str) for row in rows for cell in row):
        raise ValueError('확정되지 않은 셀은 Markdown 표로 변환할 수 없습니다.')
    width = len(rows[0])
    escaped = [[escape_markdown_chars(normalize_cell_breaks(cell)) for cell in row] for row in rows]
    header = _looks_like_header(rows[0]) if use_header is None else use_header
    first, body = (escaped[0], escaped[1:]) if header else ([''] * width, escaped)

    def line(cells):
        return '| ' + ' | '.join(cells) + ' |'

    text = '\n'.join([line(first), line(['---'] * width), *(line(row) for row in body)])
    validate_markdown(text, width)
    return text


# 수록된 규정의 표에서 관찰한 열 제목 단어. 새 문서에서 머리행을 놓치면 여기에 추가한다.
HEADER_WORDS = re.compile(r'구분|항목|사업자|기준|위반행위|심사사항|시설|제출|매출액')


def _looks_like_header(cells):
    """열 제목 단어가 있고 짧은 행만 머리행으로 본다. 백분율이 있으면 데이터 행으로 본다."""
    return (any(HEADER_WORDS.search(cell) for cell in cells)
            and all(len(cell) < 70 for cell in cells)
            and not any(re.search(r'\d+(?:\.\d+)?\s*%', cell) for cell in cells))


def validate_markdown(text, width):
    """GFM에서 열이 잘리거나 표 인식이 실패하는 행·구분행 불일치를 검사한다."""
    lines = text.splitlines()
    if len(lines) < 2:
        raise ValueError('Markdown 표의 머리행/구분행이 없습니다.')
    for line in lines:
        if len(re.split(r'(?<!\\)\|', line)) - 2 != width:
            raise ValueError('Markdown 표의 열 수가 일치하지 않습니다.')
    if not all(re.fullmatch(r':?-{3,}:?', cell.strip()) for cell in lines[1].strip('|').split('|')):
        raise ValueError('Markdown 표의 구분행이 잘못되었습니다.')


def _compact(value):
    return re.sub(r'\s+', '', value)


def _content_preserved(lines, rows):
    cells = [_compact(cell) for row in rows for cell in row if isinstance(cell, str)]
    for line in lines:
        for part in re.split(r'[\u2500-\u257f]', line):
            value = _compact(part)
            if value and not any(value in cell for cell in cells):
                return False
    return True


def _plain_frame(lines):
    """한 칸짜리 설명틀은 바깥 테두리만 걷어 문장과 내부 표를 남긴다."""
    result = []
    for line in lines:
        value = line.strip()
        if value.startswith(('│', '┃', '║')) and value.endswith(('│', '┃', '║')):
            result.append(value[1:-1].strip())
        elif _border(value):
            result.append('')
        else:
            result.append(line)
    return '\n'.join(result)


def _literal_block(lines):
    """해석할 수 없는 서식은 원문을 코드 블록에 보존해 Markdown 오인식을 막는다."""
    text = '\n'.join(lines)
    fence = '`' * max(3, max((len(m[0]) + 1 for m in re.finditer(r'`+', text)), default=3))
    return fence + 'text\n' + text + '\n' + fence


def strip_decorative_frames(value):
    """격자가 아닌 직인 틀·절차도의 선은 걷고 글자는 읽는 순서대로 남긴다."""
    if not re.search(r'[\u2500-\u257f]', value):
        return value
    return ' / '.join(part.strip() for part in re.split(r'[\u2500-\u257f]+', value) if part.strip())


def process_tables(text, _depth=0):
    """본문 순서를 유지해 표를 Markdown 문자열로 바꾸고 (문자열, 검토 사유)를 반환한다."""
    lines = text.splitlines()
    output, reviews, index = [], [], 0
    while index < len(lines):
        value = lines[index].strip()
        framed_head = re.search(r'[┏┌╔][\u2500-\u257f\s]+$', value)
        framed = framed_head is not None
        if framed:
            prefix = value[:framed_head.start()].strip()
            if prefix:
                output.append(prefix)
            value = framed_head[0]
        opening = _open_top(value) if not framed else None
        if not framed and not opening:
            output.append(strip_decorative_frames(lines[index]))
            index += 1
            continue
        start, depth, nested = index, 1, False
        index += 1
        while index < len(lines):
            current = lines[index].strip()
            if framed:
                if current.startswith(('┏', '┌', '╔')) and _border(current):
                    depth += 1
                    nested = True
                elif current.startswith(('┗', '└', '╚')) and _border(current):
                    depth -= 1
                    if not depth:
                        break
            elif re.fullmatch(r'[─━═]+(?:[┴┻┷┸┹┺╧╨╩][─━═]+)+', current):
                depth = 0
                break
            elif _open_top(current):
                break
            index += 1
        closed = depth == 0
        end = index + int(closed)
        block = [value, *lines[start + 1:end]]
        index = end
        if not closed:
            output.extend(['', _literal_block(block), ''])
            reviews.append({'line': start + 1, 'reason': '표 원문 보존: 닫는 경계를 확인할 수 없음'})
            continue
        top_width = len(re.findall('[┬┳┯┰┱┲╤╥╦]', value)) + 1
        if framed and top_width == 1 and _depth < 4:
            inner, warnings = process_tables(_plain_frame(block[1:-1]), _depth + 1)
            output.extend(['', inner, ''])
            # 안쪽 텍스트의 1행은 바깥 틀 다음 줄(start + 2행)이다.
            reviews.extend({'line': start + 1 + warning['line'], 'reason': warning['reason']}
                           for warning in warnings)
            continue
        parse_lines, parse_top, parse_bottom = block[1:-1], value, block[-1].strip()
        if not framed:
            # 좌우 외곽선이 생략된 서식도 동일한 격자 해석을 사용한다.
            parse_top = '┌' + opening[0] + '┐'
            parse_bottom = '└' + parse_bottom + '┘'
            parse_lines = [('├' + line.strip() + '┤') if
                           _border(line.strip()) and line.strip().startswith(('─', '━', '═'))
                           else ('│' + line.strip() + '│') for line in parse_lines]
        if nested:
            rows, error = None, '중첩 격자의 소속을 확정할 수 없음'
        else:
            rows, error = extract_rows(parse_lines, parse_top, parse_bottom)
        if rows is None or error or not _content_preserved(block[1:-1], rows):
            output.extend(['', _literal_block(block), ''])
            reviews.append({'line': start + 1, 'reason': f'표 원문 보존: {error or "셀 내용 보존 검사 불일치"}'})
        else:
            prefix = value[:opening.start()].strip() if opening else ''
            grids = extract_grid_sections(parse_lines, parse_top, parse_bottom, rows)
            # 나눈 구간은 모두 빈 머리행으로 시작한다(use_header=False).
            rendered = ('\n\n'.join(render_markdown(grid, use_header=False) for grid in grids)
                        if grids else render_markdown(rows))
            output.extend([prefix, '', rendered, ''])
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(output)).strip(), reviews
