"""규정 JSON의 본문을 조·항 검색용 JSON으로 변환한다.

실행: python make_json_paragraphs.py output/regulations output/paragraphs
"""
import argparse
import json
from pathlib import Path
import unicodedata

if __package__:
    from .make_json_regulations import collect_text, validate_records, write_json
else:
    from make_json_regulations import collect_text, validate_records, write_json


def make_paths(article, paragraph=None):
    parents = article.get('parents', {})
    headings = [parents[key] for key in ('part', 'chapter', 'section') if key in parents]
    parts = [article['document_name'], *headings, article['label']]
    article_path = ' '.join(parts)
    provision_path = article_path + (' ' + paragraph['label'] if paragraph else '')
    return article_path, provision_path


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
            article_path, provision_path = make_paths(article)
            output.append({'article_path': article_path, 'provision_path': provision_path,
                           'content': collect_text(article)})
            continue
        other = [child for child in article['children'] if child['type'] != 'paragraph']
        if other:
            raise ValueError(f"{article['label']}: 항과 직접 호·목이 혼재하여 검색 단위 확인 필요")
        # 조 제목 뒤에 항 번호 없이 오는 문장은 모든 항에 공통 맥락으로 붙인다.
        prefix = article['text'][len(article['label']):].strip()
        for paragraph in paragraphs:
            article_path, provision_path = make_paths(article, paragraph)
            content = '\n'.join(filter(None, [prefix, collect_text(paragraph)]))
            output.append({'article_path': article_path, 'provision_path': provision_path,
                           'content': content})
    errors = validate_paragraphs(output)
    if errors:
        raise ValueError(f'검색 JSON 오류: {errors[:5]}')
    return output


def validate_paragraphs(records):
    errors, seen = [], set()
    if not isinstance(records, list) or not records:
        return ['비어 있지 않은 검색 레코드 목록이 필요합니다.']
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {'article_path', 'provision_path', 'content'}:
            errors.append(f'{index}: 검색 레코드의 키가 올바르지 않습니다.')
            continue
        if any(not isinstance(value, str) or not value.strip() for value in record.values()):
            errors.append(f'{index}: 비어 있지 않은 문자열이 필요합니다.')
            continue
        path = record['provision_path']
        if path in seen:
            errors.append(f'{index}: 검색 경로 중복 {path}')
        seen.add(path)
        if path != record['article_path'] and not path.startswith(record['article_path'] + ' 제'):
            errors.append(f'{index}: 조·항 경로 불일치')
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
