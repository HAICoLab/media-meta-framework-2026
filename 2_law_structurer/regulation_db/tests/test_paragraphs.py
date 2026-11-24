"""검색 단위·원문 번호·본문 범위 및 실제 파일 입출력을 검증한다."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import make_json_regulations as reg
import make_json_paragraphs as para


class Paragraphs(unittest.TestCase):
    def setUp(self):
        self.source = ROOT / 'data/가상광고_세부기준_등에_관한_고시.txt'
        self.records = reg.run(self.source)['records']
        self.paragraphs = para.build_paragraphs(self.records)

    def test_only_body_and_exact_fields(self):
        self.assertEqual(len(self.paragraphs), 18)
        for record in self.paragraphs:
            self.assertEqual(list(record), para.FIELDS)
            self.assertNotIn('>', record['provision_path'])
            self.assertNotIn('부칙', record['provision_path'])
            self.assertNotIn('별표', record['provision_path'])

    def test_article_without_paragraph_is_one_record_with_its_items(self):
        article = next(r for r in self.records if r['label'].startswith('제2조('))
        result = next(r for r in self.paragraphs if ' 제2조(' in r['provision_path'])
        self.assertEqual(result['id'], result['article_id'])
        self.assertTrue(result['provision_path'].endswith(article['label']))
        self.assertEqual(result['content'], reg.collect_text(article))
        self.assertIn('1.', result['content'])
        self.assertIn('가.', result['content'])

    def test_explicit_paragraphs_preserve_markers_and_descendants(self):
        article = next(r for r in self.records if r['label'].startswith('제5조('))
        results = [r for r in self.paragraphs if ' 제5조(' in r['provision_path']]
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]['content'].startswith('①'))
        self.assertEqual(results[0]['content'], reg.collect_text(article['children'][0]))
        self.assertTrue(results[1]['provision_path'].endswith(' 제2항'))

    def test_ids_cover_every_unit_once(self):
        def ids(node):
            return [node['id'], *(i for c in node['children'] for i in ids(c))]
        expected = [i for r in self.records if r['region'] == 'body' for i in ids(r)]
        placed = [i for r in self.paragraphs for i in r['rule_ids']]
        split = {r['article_id'] for r in self.paragraphs if r['id'] != r['article_id']}
        self.assertEqual(len(placed), len(set(placed)))
        self.assertEqual(sorted(set(expected) - split), sorted(placed))
        result = next(r for r in self.paragraphs if ' 제5조(' in r['provision_path'])
        self.assertEqual(result['id'], result['article_id'] + '_P1')

    def test_optional_parents_are_in_path_in_order(self):
        article = copy.deepcopy(self.records[0])
        article['parents'] = {'section': '제1절 범위', 'part': '제1편 총칙', 'chapter': '제1장 일반'}
        path = para.make_path(article)
        self.assertIn('제1편 총칙 제1장 일반 제1절 범위 제1조', path)

    def test_duplicate_search_paths_are_rejected(self):
        self.assertTrue(para.validate_paragraphs(self.paragraphs + self.paragraphs[:1]))
        self.assertTrue(para.validate_paragraphs([{'provision_path': [], 'content': None}]))
        record = next(r for r in self.paragraphs if r['id'] != r['article_id'])
        wrong_id = record['article_id'] + '_P9'                 # 경로는 다른 항을 가리키는데 ID만 바뀐 기록
        wrong = dict(record, id=wrong_id, rule_ids=[wrong_id, *record['rule_ids'][1:]])
        self.assertTrue(any('ID와 경로' in e for e in para.validate_paragraphs([wrong])))

    def test_two_cli_steps_and_source_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / 'regulations', Path(folder) / 'paragraphs'

            def run(args):
                return subprocess.run([sys.executable, '-B', *map(str, args)], cwd=ROOT,
                                      capture_output=True, text=True)

            self.assertEqual(run(['make_json_regulations.py', self.source, first]).returncode, 0)
            self.assertEqual(run(['make_json_paragraphs.py', first, second]).returncode, 0)
            self.assertEqual(json.loads(next(second.glob('*.json')).read_text()), self.paragraphs)
            before = next(first.glob('*.json')).read_bytes()
            self.assertNotEqual(run(['make_json_paragraphs.py', first, first]).returncode, 0)
            self.assertEqual(next(first.glob('*.json')).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
