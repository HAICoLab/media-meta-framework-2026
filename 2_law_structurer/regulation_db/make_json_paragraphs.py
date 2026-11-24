"""규정 JSON의 본문을 조·항 검색용 JSON으로 변환한다.

실행: python make_json_paragraphs.py output/regulations output/paragraphs
레코드: id(항 ID, 항이 없는 조는 조 ID), article_id(소속 조), provision_path(문서명·편장절·조·항 경로), content,
rule_ids(이 레코드에 든 모든 조항 ID: 자신과 하위 호·목).
"""
import argparse
import json
from pathlib import Path
import re
import unicodedata

if __package__:
    from .make_json_regulations import collect_text, validate_records, write_json
else:
    from make_json_regulations import collect_text, validate_records, write_json


FIELDS = ['id', 'article_id', 'provision_path', 'content', 'rule_ids']


def collect_ids(node):
    return [node['id'], *(i for child in node['children'] for i in collect_ids(child))]


def make_path(article, paragraph=None):
    """문서명, 상위 편·장·절, 조 이름(항이 있으면 항 이름까지)을 공백으로 이은 경로."""
    parents = article.get('parents', {})
    headings = [parents[key] for key in ('part', 'chapter', 'section') if key in parents]
    parts = [article['document_name'], *headings, article['label']]
    return ' '.join(parts + ([paragraph['label']] if paragraph else []))


def build_paragraphs(records):
    """본문 항과 하위 호·목을 합친다. 항이 없는 조는 전체를 한 레코드로 둔다."""
    errors = validate_records(records)
    if errors:
        raise ValueError(f'입력 규정 JSON 오류: {errors[:5]}')
    output = []
    for article in records:
        if article['region'] != 'body':
            continue
        paragraphs = [child for child in article['children'] if child['type'] == 'paragraph']
        if not paragraphs:
            output.append({'id': article['id'], 'article_id': article['id'], 'provision_path': make_path(article),
                           'content': collect_text(article), 'rule_ids': collect_ids(article)})
            continue
        other = [child for child in article['children'] if child['type'] != 'paragraph']
        if other:
            raise ValueError(f"{article['label']}: 항과 직접 호·목이 혼재하여 검색 단위 확인 필요")
        # 조 제목 뒤에 항 번호 없이 오는 문장은 모든 항에 공통 맥락으로 붙인다.
        prefix = article['text'][len(article['label']):].strip()
        for paragraph in paragraphs:
            content = '\n'.join(filter(None, [prefix, collect_text(paragraph)]))
            output.append({'id': paragraph['id'], 'article_id': article['id'],
                           'provision_path': make_path(article, paragraph),
                           'content': content, 'rule_ids': collect_ids(paragraph)})
    errors = validate_paragraphs(output)
    if errors:
        raise ValueError(f'검색 JSON 오류: {errors[:5]}')
    return output


def belongs(rule_id, article_id):
    """rule_id가 article_id 조의 ID인가. ART1과 ART10, ART38과 ART38_2처럼 앞부분만 같은 다른 조는 거른다."""
    return isinstance(rule_id, str) and (rule_id == article_id
                                         or re.match(re.escape(article_id) + r'_(?:P\d|I\d|[A-Z]+$|[A-Z]+_)', rule_id) is not None)


def validate_paragraphs(records):
    errors, seen, ids = [], set(), set()
    if not isinstance(records, list) or not records:
        return ['비어 있지 않은 검색 레코드 목록이 필요합니다.']
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != set(FIELDS):
            errors.append(f'{index}: 검색 레코드의 키가 올바르지 않습니다.')
            continue
        if any(not isinstance(record[key], str) or not record[key].strip() for key in FIELDS if key != 'rule_ids'):
            errors.append(f'{index}: 비어 있지 않은 문자열이 필요합니다.')
            continue
        rule_ids = record['rule_ids']
        if (not isinstance(rule_ids, list) or not rule_ids or rule_ids[0] != record['id']
                or any(not belongs(i, record['article_id']) for i in rule_ids)):
            errors.append(f'{index}: rule_ids는 자신의 ID로 시작하고 같은 조의 ID만 담아야 합니다.')
            continue
        if ids & set(rule_ids):
            errors.append(f'{index}: 조항 ID가 두 레코드에 들어 있음')
        ids.update(rule_ids)
        path = record['provision_path']
        if path in seen:
            errors.append(f'{index}: 검색 경로 중복 {path}')
        seen.add(path)
        paragraph = re.fullmatch(re.escape(record['article_id']) + r'_P(\d+)', record['id'])
        if record['id'] != record['article_id'] and not (paragraph and path.endswith(f' 제{paragraph[1]}항')):
            errors.append(f'{index}: ID와 경로가 맞지 않음 (조 또는 그 조의 항이어야 함)')
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--overwrite', action='store_true', help='기존 결과와 내용이 달라도 덮어쓴다')
    args = parser.parse_args()
    paths = sorted(args.input.glob('*.json')) if args.input.is_dir() else [args.input]
    if not paths:
        parser.error('입력 규정 JSON이 없습니다.')
    failed = False
    for path in paths:
        try:
            records = json.loads(path.read_text(encoding='utf-8'))
            paragraphs = build_paragraphs(records)
            output = args.output / unicodedata.normalize('NFC', path.name)
            if output.resolve() == path.resolve():
                raise ValueError('1차 규정 JSON을 2차 출력으로 덮어쓸 수 없습니다.')
            write_json(paragraphs, output, overwrite=args.overwrite)
            print(json.dumps({'file': path.name, 'records': len(paragraphs), 'status': 'passed'},
                             ensure_ascii=False))
        except (ValueError, OSError) as error:
            failed = True
            print(json.dumps({'file': path.name, 'status': 'failed', 'error': str(error)},
                             ensure_ascii=False))
    raise SystemExit(int(failed))


if __name__ == '__main__':
    main()
