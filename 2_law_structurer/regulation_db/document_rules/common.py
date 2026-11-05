"""행정규칙과 법령이 공유하는 문서 영역·번호·첨부 정보 규칙."""
import re
from datetime import datetime


# 항 번호 기호: ①~⑳, ㉑~㉟, ㊱~㊿
MARKERS = ''.join(chr(n) for a, b in ((0x2460, 0x2474), (0x3251, 0x3260),
                                      (0x32B1, 0x32C0)) for n in range(a, b))
ARTICLE = re.compile(r'^제\d+조(?:의\d+)?\s*\(')
HEADING = re.compile(r'^제\d+(편|장|절)(?:의\d+)?\s+.+$')
PARAGRAPH = re.compile(r'^([' + MARKERS + r'])\s*(.*)$')
# '1.5배'처럼 소수점 뒤에 숫자가 이어지면 호 번호가 아니다.
ITEM = re.compile(r'^(\d+(?:의\d+)?)\.(?!\d)\s*(.*)$')
# 목 번호는 가나다 순서의 글자만 쓴다.
SUBITEM = re.compile(r'^([가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허])\.\s*(.*)$')
DELETED = re.compile(r'^(제\d+조(?:의\d+)?)\s+(삭\s*제)(.*)$')
SUPPLEMENT = re.compile(r'^(\d{8})\s+(\d+(?:-\d+)*)\s+부칙(?=\s|\(|$)')
NOTICE = re.compile(r'^⊙.*(?:고시|공고|규칙|법률|대통령령|부령)\s*제\d+(?:-\d+)*호(?:\s*\([^\n]*\))?\s*$')
DOWNLOAD = re.compile(r'^(?:https?://(?:www\.)?law\.go\.kr)?/(?:LSW/)?flDownload\.do\?\S+$', re.I)
FILE = re.compile(r'^.+\.(?:hwp|hwpx|pdf|gif|png|jpe?g)\s*$', re.I)
APPENDIX = re.compile(r'\[(별표|별지|서식|표)\s*(?:제\s*)?(\d+)(?:\s*(?:호)?의\s*(\d+))?[^\]]*\]')
ANNOTATION = re.compile(r'^\[(?:본조신설|전문개정|제목개정|종전\s|제\d+조|시행일|법률\s+제|\d{4}\.\d{1,2}\.\d{1,2}\s+법률).*\]$')


def read_header(rows):
    """수집 머리말의 문서명·출처·판본 정보를 읽는다."""
    keys = {'소관': 'authority', '시행일': 'effective_date', '일련번호': 'serial',
            '법령마스터': 'serial', '출처': 'origin', '수집일': 'collected_date'}
    metadata = {}
    for row in rows:
        value = row['text'].strip()
        if not value:
            continue
        if not value.startswith('#'):
            break
        value = value.lstrip('#').strip()
        if ':' not in value:
            metadata.setdefault('document_name', value)
        else:
            for field in value.split('|'):
                key, sep, content = field.strip().partition(':')
                if sep and key in keys:
                    target, content = keys[key], content.strip()
                    if target in metadata and metadata[target] != content:
                        raise ValueError(f"{row['source_line']}행: 상충하는 머리말 {key}")
                    metadata[target] = content
    if not metadata.get('document_name'):
        raise ValueError('문서명 머리말이 없습니다.')
    return metadata


def date_string(value):
    if not value:
        return None
    if not re.fullmatch(r'\d{8}', value):
        raise ValueError(f'날짜 형식 오류: {value}')
    return datetime.strptime(value, '%Y%m%d').strftime('%Y-%m-%d')


def heading(text):
    match = HEADING.fullmatch(text.strip())
    return ({'편': 'part', '장': 'chapter', '절': 'section'}[match[1]], text.strip()) if match else None


def article_label(text):
    """괄호가 중첩된 조 제목도 닫는 괄호까지 읽는다."""
    match = ARTICLE.match(text)
    if not match:
        deleted = DELETED.match(text)
        return deleted[1] if deleted else None
    depth = 1
    for i in range(match.end(), len(text)):
        depth += (text[i] == '(') - (text[i] == ')')
        if not depth:
            return text[:i + 1]
    raise ValueError(f'닫히지 않은 조 제목: {text[:80]}')


def identify_nodes(text):
    """번호를 label로 식별하되 text에는 원문 번호 기호를 남긴다."""
    value = text.strip()
    label = article_label(value)
    if label:
        rest = value[len(label):].lstrip()
        node = {'type': 'article', 'label': label, 'text': value, 'children': []}
        if PARAGRAPH.match(rest):
            node['text'] = label
            return [node, *identify_nodes(rest)]
        return [node]
    match = PARAGRAPH.match(value)
    if match:
        return [{'type': 'paragraph', 'label': f'제{MARKERS.index(match[1]) + 1}항',
                 'text': value, 'children': []}]
    for pattern, kind in ((ITEM, 'item'), (SUBITEM, 'subitem')):
        match = pattern.match(value)
        if match:
            label = f'제{match[1]}호' if kind == 'item' else f'{match[1]}목'
            return [{'type': kind, 'label': label, 'text': value, 'children': []}]
    return []


def supplement_start(rows, index):
    values = [r['text'].strip() for r in rows[index:index + 3]]
    match = SUPPLEMENT.match(values[0])
    if match:
        return f'부칙({match[1]}, {match[2]})'
    if (len(values) == 3 and re.fullmatch(r'\d{8}', values[0])
            and re.fullmatch(r'\d+(?:-\d+)*', values[1])
            and re.match(r'^부칙(?:\s|\(|$)', values[2])):
        return f'부칙({values[0]}, {values[1]})'
    return None


def appendix_start(rows, index):
    values = [r['text'].strip() for r in rows[index:index + 3]]
    if (len(values) == 3 and re.fullmatch(r'\d{4}', values[0])
            and re.fullmatch(r'\d{2}', values[1]) and values[2] in {'별표', '별지', '서식'}):
        number = str(int(values[0])) + (f'의{int(values[1])}' if int(values[1]) else '')
        return f"{'별지' if values[2] == '서식' else values[2]}{number}"
    return None


def amendment_start(rows):
    """개정문 머리말을 확인한 뒤 공포 서두를 포함해 잘라낼 위치를 반환한다."""
    for i, row in enumerate(rows):
        if NOTICE.fullmatch(row['text'].strip()):
            evidence = ' '.join(r['text'] for r in rows[i + 1:i + 9])
            if '개정' not in evidence:
                raise ValueError(f"{row['source_line']}행: 개정문 시작을 확인할 수 없습니다.")
            for j in range(max(0, i - 8), i):
                if re.match(r'^국회에서 의결된 .+개정법률을 이에 공포한다\.', rows[j]['text'].strip()):
                    return j
            return i
    return len(rows)


def appendix_marker(value):
    """'[별표 1] 제목'이나 '■ 법령명 [별지 제1호 서식]'처럼 첨부의 시작을 알리는 줄이면 표시를 반환한다.

    '[표 1]은 다음과 같다'처럼 표시 뒤에 글자가 바로 붙는 본문 문장은 제외한다.
    """
    if not value.startswith(('[', '■')):
        return None
    marker = APPENDIX.search(value)
    if marker and (marker.end() == len(value) or value[marker.end()].isspace()):
        return marker
    return None


def split_sections(prepared):
    """개정문 전까지의 본문·날짜별 부칙·별표/별지를 나눈다."""
    rows = prepared['lines']
    cut = amendment_start(rows)
    excluded = [{**r, 'reason': 'amendment_tail'} for r in rows[cut:]]
    sections = [{'region': 'body', 'region_label': '본문', 'lines': []}]
    i = 0
    while i < cut:
        row, value = rows[i], rows[i]['text'].strip()
        supplement = supplement_start(rows, i)
        appendix = appendix_start(rows, i)
        marker = appendix_marker(value)
        if supplement:
            sections.append({'region': 'supplement', 'region_label': supplement, 'lines': []})
        elif appendix:
            sections.append({'region': 'appendix', 'region_label': appendix, 'lines': []})
        elif marker and sections[-1]['region'] != 'appendix':
            sections.append({'region': 'appendix', 'region_label': marker[0], 'lines': []})
        # 다운로드 URL과 인접한 파일명만 제거한다. 내용 중 일반 URL은 보존한다.
        adjacent = [r['text'].strip() for r in rows[max(0, i - 1):min(cut, i + 2)]]
        if DOWNLOAD.fullmatch(value) or (FILE.fullmatch(value)
                                         and any(DOWNLOAD.fullmatch(s) for s in adjacent)):
            excluded.append({**row, 'reason': 'download'})
        else:
            sections[-1]['lines'].append(row)
        i += 1
    return [s for s in sections if s['lines']], excluded


def clean_appendix(section):
    """첨부 식별자는 label에 쓰고, 확인된 중복 제목만 본문에서 제거한다."""
    rows = list(section['lines'])
    removed = []
    if appendix_start(rows, 0):
        removed.extend({**r, 'reason': 'appendix_id'} for r in rows[:3])
        rows = rows[3:]
    label = section['region_label']
    marker_index = next((i for i, r in enumerate(rows)
                         if APPENDIX.search(r['text'])
                         and r['text'].strip().startswith(('[', '■', '「'))), None)
    if marker_index is not None:
        match = APPENDIX.search(rows[marker_index]['text'])
        label = re.sub(r'\s+', '', match[0].strip('[]'))

        # API 제목과 실제 제목이 공백·줄바꿈이나 관련 조문 괄호 표기만 다르면 중복으로 보고 없앤다.
        def title_key(value):
            value = re.sub(r'\s+', '', value)
            # 관련 조문 표기의 대괄호/소괄호 차이는 같은 제목의 서식 차이다.
            return re.sub(r'\[(제[^\[\]]+관련)\]', r'(\1)', value)

        title = title_key(''.join(r['text'] for r in rows[:marker_index]))
        if title:
            following = '\n'.join(r['text'] for r in rows[marker_index + 1:marker_index + 5])
            after = rows[marker_index]['text'][match.end():] + '\n' + following
            if title_key(after).startswith(title):
                removed.extend({**r, 'reason': 'duplicate_appendix_title'} for r in rows[:marker_index])
                rows = rows[marker_index:]
    return label, rows, removed
