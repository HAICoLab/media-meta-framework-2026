"""관련 조항 원문 → 조항 ID 규칙. [규칙] 모듈.

의결의 관련 조항 문자열(예: 「방송심의에 관한 규정」제47조(간접광고)제1항제1호)을 읽어 조항 ID를 만들고,
규정 JSON(regulation_db/output/regulations)에 그 ID가 있는지 확인한다. 라벨 표기 차이에 영향받지 않는다.
읽지 못하거나 확신할 수 없는 부분은 추측하지 않고 오류로 남긴다.
"""
import json
from pathlib import Path
import re
import unicodedata


TITLE = re.compile('[「｢『](.*?)[」｣』]')
# 「」 없이 쓴 규정명. 문자열 맨 앞이나 쉼표 바로 뒤에서, '제N조'가 아닌 글자로 시작해 규정 종류 이름으로 끝난다.
BARE_TITLE = re.compile(r'(?:^|(?<=,))(?!제\d)([^「」｢｣『』,()]*?(?:규정|규칙|고시|기준|법률|시행령|시행규칙|법))(?=\(|제\d)')
QUOTED_IN_PARENS = re.compile(r'\([^()]*[「｢『][^()]*\)')                  # "(「식품표시광고법」 위반)" 같은 설명 괄호
HEADING = re.compile(r'제(\d+)조(?:의(\d+))?\(([^)]*)\)')                 # "제58조의2(방송사고)"의 조 제목
# 가지 번호는 "제4호의2"와 "제4의2호" 두 표기를 모두 읽는다. 목은 "가목", "가·나목"을 읽는다.
TOKEN = re.compile(r'제(?P<number>\d+)(?:의(?P<pre>\d+))?(?P<unit>조|항|호)(?:의(?P<sub>\d+))?'
                   r'|(?P<letters>[가-힣](?:[·ㆍ][가-힣])*)목')
RANGE = re.compile('부터|내지|[~∼～]')
ORDER = {'조': 0, '항': 1, '호': 2, '목': 3}
SUBITEM_ORDER = '가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허'
TITLE_ALIASES = {'방송심의규정': '방송심의에관한규정', '방송광고심의규정': '방송광고심의에관한규정'}


def compact(value):
    return re.sub(r'[\s_]+', '', unicodedata.normalize('NFC', value or '')).replace('ㆍ', '·')


def title_key(value):
    key = compact(value).strip('「」｢｣『』')
    key = re.sub(r'\(.*?\)$', '', key)                 # "(규칙 제150호)" 같은 판본 표기
    return TITLE_ALIASES.get(key, key)


def subitem_letter(letter):
    """가목 → A, 나목 → B, …, 하목 → N, 거목 → O, … (조항 ID의 목 표기)"""
    index = SUBITEM_ORDER.index(letter)
    return chr(ord('A') + index) if index < 26 else 'A' + chr(ord('A') + index - 26)


def find_titles(text):
    """규정명 위치 목록 [(시작, 끝, 규정명)]. 「」 안의 이름과 「」 없이 쓴 이름을 함께 찾는다."""
    found = [(m.start(), m.end(), m[1]) for m in TITLE.finditer(text)]
    for m in BARE_TITLE.finditer(text):
        if not any(s <= m.start(1) < e for s, e, _ in found):
            found.append((m.start(1), m.end(1), m[1]))
    return sorted(found)


def parse_references(text):
    """'「규정명」제47조제1항제3호, 제2항제2호' 같은 문자열을 (규정명, {조,항,호,목}) 목록으로 읽는다.

    조 제목("제58조의2(방송사고)"의 "방송사고")은 headings에 따로 모은다.
    """
    normalized = QUOTED_IN_PARENS.sub('', compact(text))
    titles = find_titles(normalized)
    refs, errors, headings = [], [], {}
    if not titles:
        return refs, ['규정명이 명시되지 않음'], headings
    if TOKEN.search(normalized[:titles[0][0]]):
        errors.append(f'규정명 앞의 조항: {normalized[:titles[0][0]]}')
    for i, (_, end, title) in enumerate(titles):
        fragment = normalized[end:titles[i + 1][0] if i + 1 < len(titles) else len(normalized)]
        for number, sub, heading in HEADING.findall(fragment):
            headings[(title, number + (f'_{sub}' if sub else ''))] = heading
        path, found, after_range = {}, False, False

        def emit():
            if '조' in path and (title, dict(path)) not in refs:
                refs.append((title, dict(path)))

        for part in re.sub(r'\([^)]*\)', '', fragment).split(','):   # 조 제목·판본 표기 괄호는 뗀다
            if RANGE.search(part):
                errors.append(f'범위 인용: {title} {part}')
                emit()
                path, after_range = {}, True
                continue
            for token in TOKEN.finditer(part):
                unit = '목' if token['letters'] else token['unit']
                if unit != '조' and '조' not in path:
                    errors.append(f'조 없이 쓴 {unit}: {title} {part}' if after_range or found
                                  else f'조 번호를 읽지 못함: {title} {part}')
                    continue
                letters = token['letters'].replace('ㆍ', '·').split('·') if token['letters'] else [None]
                if unit == '목' and any(letter not in SUBITEM_ORDER for letter in letters):
                    errors.append(f'목 표기를 읽지 못함: {title} {token[0]}')
                    continue
                for letter in letters:
                    if any(ORDER[k] >= ORDER[unit] for k in path):
                        emit()
                        path = {k: v for k, v in path.items() if ORDER[k] < ORDER[unit]}
                    sub = token['pre'] or token['sub']
                    path[unit] = letter or token['number'] + (f'_{sub}' if sub else '')
                found = True
                if unit == '조':
                    after_range = False
        emit()
        if not found and not any(e.startswith(f'범위 인용: {title}') for e in errors):
            errors.append(f'조 번호를 읽지 못함: {title}')
    return refs, errors, headings


def reference_id(article_id, path):
    """조 ID + {항: '1', 호: '4_2', 목: '가'} → …_P1_I4_2_A"""
    node_id = article_id
    if '항' in path:
        node_id += f"_P{path['항']}"
    if '호' in path:
        node_id += f"_I{path['호']}"
    if '목' in path:
        node_id += '_' + subitem_letter(path['목'])
    return node_id


def cited(path):
    """{조: '58_2', 항: '1'} → '제58조의2 제1항'"""
    out = []
    for unit in ORDER:
        if unit in path:
            number, _, sub = path[unit].partition('_')
            out.append(f'{number}목' if unit == '목' else f'제{number}{unit}' + (f'의{sub}' if sub else ''))
    return ' '.join(out)


def heading_of(label):
    match = re.search(r'\((.*)\)$', label)
    return compact(match[1]) if match else ''


def is_deleted(node):
    """"삭제"된 조·항·호. 정답이 될 수 없다."""
    text = re.sub(r'<[^>]*>|\[[^\]]*\]', '', node['text'])
    return not node['children'] and bool(re.fullmatch(r'(?:제\d+조(?:의\d+)?|[①-⑳]|\d+\.)?\s*삭\s*제\s*', text.strip()))


class Regulations:
    """규정 JSON(파일 하나 또는 문서별 파일이 든 폴더)의 조항 ID 목록. 본문 조만 쓴다."""

    def __init__(self, path):
        path = Path(path)
        files = sorted(path.glob('*.json')) if path.is_dir() else [path]
        if not path.exists() or not files:
            raise SystemExit(f'규정 JSON을 찾을 수 없습니다: {path}')
        try:
            self.records = [r for f in files for r in json.loads(f.read_text(encoding='utf-8'))
                            if isinstance(r, dict) and r.get('id') and r.get('type') == 'article']
        except (json.JSONDecodeError, AttributeError) as error:
            raise SystemExit(f'규정 JSON 형식이 아닙니다: {path} ({error})')
        if not self.records:
            raise SystemExit(f'조항 ID가 붙은 규정 JSON이 아닙니다(항 검색 JSON을 준 것은 아닌지 확인): {path}')
        self.nodes, self.prefix = {}, {}
        for record in self.records:
            # 규정 JSON마다 문서명 필드 이름이 다르다(ETRI: document_name, 논문: document).
            document = record.get('document_name') or record.get('document')
            self.prefix[title_key(document)] = record['id'].split('_ART')[0]
            self._index(record)

    def _index(self, node):
        self.nodes[node['id']] = node
        for child in node['children']:
            self._index(child)

    def by_heading(self, prefix, heading):
        """같은 규정에서 조 제목이 정확히 같은 조. 하나일 때만 돌려준다."""
        found = [r for r in self.records
                 if r['id'].split('_ART')[0] == prefix and heading_of(r['label']) == compact(heading)]
        return found[0] if len(found) == 1 else None

    def resolve(self, text):
        """관련조항 문자열 → (조항 ID 목록, 오류 목록, 기록).

        조 번호가 규정에 없으면 인용에 적힌 조 제목으로 찾는다. 제목이 정확히 한 조와 같을 때만 연결하고 기록에
        남긴다(예: 의결의 "제58조의2(방송사고)" → 규정의 "제55조의2(방송사고)"). 번호로 찾은 조의 제목이 인용과
        다르면 연결은 하되 기록에 남긴다. 항·호·목은 보정하지 않으며, 삭제된 조항은 정답으로 쓰지 않는다.
        """
        if not text or text.strip() in {'-', '—', '없음'}:
            return [], ['관련조항 없음'], []
        refs, errors, headings = parse_references(text)
        ids, notes = [], []
        for title, path in refs:
            prefix = self.prefix.get(title_key(title))
            if not prefix:
                errors.append(f'대상 규정이 아님: {title}')
                continue
            heading = headings.get((title, path['조']))
            article = self.nodes.get(f"{prefix}_ART{path['조']}")
            if article is None and heading:
                article = self.by_heading(prefix, heading)
                if article is not None:
                    notes.append(f"조 번호 보정: {cited({'조': path['조']})}({heading}) → {article['label']}")
            elif article is not None and heading and heading_of(article['label']) != compact(heading):
                notes.append(f"조 제목 다름: 인용 {cited({'조': path['조']})}({heading}) / 규정 {article['label']}")
            if article is None:
                errors.append(f"없는 조: {title} {cited({'조': path['조']})}")
                continue
            node = self.nodes.get(reference_id(article['id'], path))
            if node is None:
                errors.append(f'없는 단위: {title} {cited(path)}')
                continue
            if is_deleted(node):
                errors.append(f'삭제된 조항: {title} {cited(path)}')
                continue
            if node['id'] not in ids:
                ids.append(node['id'])
        return ids, errors, notes
