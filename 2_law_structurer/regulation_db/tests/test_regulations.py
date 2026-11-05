"""실제 23개 문서의 보존·경계·계층과 잘못된 입력을 검증한다."""
import copy
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import make_json_regulations as reg
from document_rules import administrative_rule, law_rule, common


class Regulations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {p.stem: reg.run(p) for p in sorted((ROOT / 'data').glob('*.txt'))}

    def result(self, name='가상광고_세부기준_등에_관한_고시'):
        return copy.deepcopy(self.results[name])

    def test_all_documents_and_body_counts(self):
        self.assertEqual(len(self.results), 23)
        counts = [sum(r['region'] == 'body' for r in result['records']) for result in self.results.values()]
        self.assertEqual(sum(counts), 687)
        for name, result in self.results.items():
            with self.subTest(name=name):
                self.assertNotEqual(result['validation']['status'], 'failed')
                self.assertEqual(result['validation']['issues'], [])
                self.assertEqual(reg.validate_records(result['records']), [])
                self.assertEqual(result['trace']['source']['raw_text'], (ROOT / 'data' / (name + '.txt')).read_text())

    def test_law_dates_articles_and_api_cleanup(self):
        for suffix, count, date in [('', 131, '2026-05-12'), ('_시행령', 55, '2026-03-24'), ('_시행규칙', 42, '2026-05-12')]:
            result = self.result('영화_및_비디오물의_진흥에_관한_법률' + suffix)
            body = [r for r in result['records'] if r['region'] == 'body']
            self.assertEqual(len(body), count)
            self.assertEqual(body[0]['source']['effective_date'], date)
            self.assertFalse(re.search(r'(?m)^(?:조문|전문|N|Y|\d{8})$', '\n'.join(map(reg.collect_text, body))))

    def test_rule_selection_uses_source_not_filename_or_topic(self):
        self.assertIs(reg.select_rules('# 영화 고시\n# 출처: API (target=admrul)'), administrative_rule)
        self.assertIs(reg.select_rules('# 방송법\n# 출처: API (target=law)'), law_rule)
        self.assertIs(reg.select_rules('# 규정\n# 출처: 방송미디어통신위원회 행정규칙집 수록'), administrative_rule)
        for text in ['# 영화 법률', '# 출처: (target=law) (target=admrul)']:
            with self.assertRaises(ValueError):
                reg.select_rules(text)

    def test_regions_exclude_amendment_tail_and_downloads(self):
        for result in self.results.values():
            self.assertTrue(set(r['region'] for r in result['records']) <= {'body', 'supplement', 'appendix'})
            text = '\n'.join(map(reg.collect_text, result['records']))
            self.assertNotIn('flDownload.do?', text)
            self.assertFalse(any(common.NOTICE.fullmatch(line) for line in text.splitlines()))
            for row in result['trace']['excluded']:
                self.assertIn(row['reason'], {'download', 'amendment_tail', 'appendix_id', 'duplicate_appendix_title'})

    def test_all_movie_amendment_heads_are_excluded(self):
        for suffix in ('', '_시행령', '_시행규칙'):
            result = self.result('영화_및_비디오물의_진흥에_관한_법률' + suffix)
            excluded = [r for r in result['trace']['excluded'] if r['reason'] == 'amendment_tail']
            self.assertTrue(excluded, suffix)
            self.assertTrue(any(r['text'].startswith('⊙') for r in excluded), suffix)
            self.assertNotIn('국회에서 의결된', '\n'.join(map(reg.collect_text, result['records'])))
        for value in ('⊙법률 제21093호', '⊙대통령령 제36220호(2026.3.24)', '⊙문화체육관광부령 제632호'):
            self.assertIsNotNone(common.NOTICE.fullmatch(value), value)

    def test_supplement_and_appendix_can_exist_inside_excluded_tail(self):
        values = ['제1조(목적) 본문', '20260101 1 부칙', '제1조(시행일) 시행한다.',
                  '⊙문화체육관광부령 제99호', '일부개정령을 다음과 같이 공포한다.',
                  '20260201 99 부칙', '제1조(시행일) 삭제 대상', '0001', '00', '별표', '삭제 표']
        prepared = {'lines': [{'source_line': i, 'text': v} for i, v in enumerate(values, 1)]}
        sections, excluded = common.split_sections(prepared)
        self.assertEqual([s['region'] for s in sections], ['body', 'supplement'])
        self.assertEqual(len(excluded), 8)

    def test_download_removal_is_limited_to_attachment_metadata(self):
        values = ['제1조(목적) 본문', '0001', '00', '별표', '[별표 1]',
                  '/LSW/flDownload.do?flSeq=1', 'attachment.pdf', '자료 https://example.org/info', 'ordinary.pdf']
        prepared = {'lines': [{'source_line': i, 'text': v} for i, v in enumerate(values, 1)]}
        sections, excluded = common.split_sections(prepared)
        self.assertEqual([r['text'] for r in excluded], values[5:7])
        self.assertEqual([r['text'] for r in sections[-1]['lines']][-2:], values[-2:])

    def test_part_chapter_section_and_context_reset(self):
        values = ['제1편 총칙', '제1장 일반', '제1절 범위', '제1조(목적) ① 내용', '제2장 기타',
                  '제2조(대상) 내용', '제2편 특칙', '제3조(정의) 내용']
        rows = [{'source_line': i, 'text': v} for i, v in enumerate(values, 1)]
        nodes, _ = reg.build_nodes(rows, administrative_rule)
        self.assertEqual(nodes[0]['parents'], {'part': '제1편 총칙', 'chapter': '제1장 일반', 'section': '제1절 범위'})
        self.assertEqual(nodes[1]['parents'], {'part': '제1편 총칙', 'chapter': '제2장 기타'})
        self.assertEqual(nodes[2]['parents'], {'part': '제2편 특칙'})
        self.assertEqual(nodes[0]['children'][0]['text'], '① 내용')

    def test_branch_numbers_and_nested_title(self):
        self.assertEqual(common.article_label('제3조의2(이용(시험)의 기준) 내용'), '제3조의2(이용(시험)의 기준)')
        self.assertEqual(common.identify_nodes('12의2. 내용')[0]['label'], '제12의2호')
        self.assertEqual(common.identify_nodes('가. 내용')[0]['text'], '가. 내용')
        self.assertEqual(common.identify_nodes('제7조 삭제 <2020.1.1.>')[0]['label'], '제7조')

    def test_annotations_are_separate_from_paragraph_text(self):
        body = self.result('영화_및_비디오물의_진흥에_관한_법률')['records']
        article = next(r for r in body if r['label'].startswith('제29조('))
        self.assertTrue(any('헌법불합치' in n['text'] for n in article['annotations']))
        self.assertNotIn('헌법불합치', reg.collect_text(article))

    def test_appendix_identity_and_duplicate_title(self):
        values = ['0001', '02', '서식', '신 고 서', '■ 문서 [별지 제1호의2서식]', '신고서', '내용 신고서']
        section = {'region_label': '별지1의2', 'lines': [{'source_line': i, 'text': v} for i, v in enumerate(values, 1)]}
        label, rows, _ = common.clean_appendix(section)
        self.assertEqual(label, '별지제1호의2서식')
        self.assertEqual([r['text'] for r in rows], values[4:])

    def test_attachment_title_bracket_variation(self):
        values = ['0003', '00', '별표', '기재사항[제21조관련]', '[별표 3]', '기재사항(제21조 관련)', '내용']
        section = {'region_label': '별표3', 'lines': [{'source_line': i, 'text': v} for i, v in enumerate(values, 1)]}
        _, rows, _ = common.clean_appendix(section)
        self.assertEqual([r['text'] for r in rows], values[4:])

    def test_damaged_source_remains_visible(self):
        result = self.result('보편적_시청권_관련_금지행위_세부기준')
        self.assertEqual(result['validation']['status'], 'review_required')
        self.assertTrue(any('번호 뒤 내용 없음' in r['reason'] for r in result['validation']['review_issues']))
        self.assertTrue(any(n['text'] == '①' for r in result['records'] for n in r['children']))

    def test_validator_rejects_invalid_types_and_duplicates(self):
        for key, value in [('label', None), ('region', []), ('source', {}), ('children', None)]:
            records = self.result()['records']
            records[0][key] = value
            self.assertTrue(reg.validate_records(records))
        records = self.result()['records']
        records.append(copy.deepcopy(records[0]))
        self.assertTrue(any('중복' in s for s in reg.validate_records(records)))
        records = self.result()['records']
        records[1]['children'][0]['label'] = []
        self.assertTrue(reg.validate_records(records))

    def test_invalid_result_cannot_overwrite_saved_file(self):
        result = self.result()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.json'
            reg.save_json(result, path)
            before = path.read_bytes()
            result['records'][0]['label'] = None
            with self.assertRaises(ValueError):
                reg.save_json(result, path, overwrite=True)
            self.assertEqual(path.read_bytes(), before)

    def test_idempotence_and_conflict_protection(self):
        result = self.result()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'result.json'
            reg.save_json(result, path)
            reg.save_json(result, path)
            self.assertEqual(json.loads(path.read_text()), result['records'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            path.write_text('keep')
            with self.assertRaises(FileExistsError):
                reg.save_json(result, path)
            self.assertEqual(path.read_text(), 'keep')

    def test_number_patterns_reject_decimals_and_non_sequence_letters(self):
        self.assertEqual(common.identify_nodes('1.5배 이상으로 한다'), [])
        self.assertEqual(common.identify_nodes('함. 이어지는 문장'), [])
        self.assertEqual(common.identify_nodes('차. 내용')[0]['label'], '차목')

    def test_table_reference_in_body_does_not_start_appendix(self):
        self.assertIsNone(common.appendix_marker('[표 1]은 다음과 같다.'))
        self.assertEqual(common.appendix_marker('[별표 1] 과징금 기준')[0], '[별표 1]')
        self.assertEqual(common.appendix_marker('■ 법 시행규칙 [별지 제1호서식]')[0], '[별지 제1호서식]')


if __name__ == '__main__':
    unittest.main()
