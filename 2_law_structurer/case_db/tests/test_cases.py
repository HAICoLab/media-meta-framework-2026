"""실제 PDF를 이용한 사례 연결·원문 보존·저장 회귀 검사."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from case_db import make_json_cases as m
from case_db import case_rules as r
ROOT = Path(m.__file__).parent


class CaseChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {name: m.run(ROOT / 'data' / name) for name in [
            '2026_06_broadcast_ad_ntt21394.pdf', '2026_06_terrestrial_ntt21395.pdf',
            '2022_07_specialty_ntt18146.pdf', '2026_09_comprehensive_news_ntt21751.pdf']}

    def sample(self): return copy.deepcopy(self.results['2026_06_broadcast_ad_ntt21394.pdf'])

    def test_actual_schema_and_missing_source_fields(self):
        result = self.sample()
        case = result['cases'][0]
        self.assertEqual(set(case), {'case_id', 'case_date', 'case_metadata', 'case_decision', 'source'})
        self.assertIsNone(case['case_metadata']['broadcaster'])
        self.assertEqual(set(case['source']), {'file', 'report_period', 'category', 'page', 'sha256'})
        self.assertFalse(m.validate_output(result['cases']))

    def test_verified_boundary_repair_and_raw_preservation(self):
        result = self.results['2026_06_terrestrial_ntt21395.pdf']
        a = next(c for c in result['cases'] if c['case_id'] == '2025-05-0076')
        b = next(c for c in result['cases'] if c['case_id'] == '2026-방송-10-0037')
        self.assertEqual(len(a['case_metadata']['broadcast_time']), 1)
        self.assertEqual(b['case_metadata']['broadcast_time'][0], {'date': '2024-07-02', 'start_time': '19:40', 'end_time': '21:00'})
        self.assertTrue(result['trace']['raw']['pages'][1]['tables'][0]['rows'][1][2].endswith('2024.07.02.화,'))
        self.assertEqual(len(result['trace']['bound']['corrections']), 1)
        with tempfile.TemporaryDirectory() as tmp:
            final = Path(tmp) / 'final.json'
            m.save_json(copy.deepcopy(result), final)
            self.assertEqual(json.loads(final.read_text()), result['cases'])
        original = m.bind_headers(result['trace']['raw'])
        self.assertEqual(r.repair_boundaries(original, {'sha256': 'different'})['rows'], original['rows'])

    def test_no_official_id_creates_distinct_stable_case_ids(self):
        result = self.results['2022_07_specialty_ntt18146.pdf']
        cases = result['cases']
        self.assertEqual(len(cases), 12)
        self.assertEqual(len({c['case_id'] for c in cases}), 12)
        self.assertTrue(all(c['case_id'].startswith('local:') for c in cases))
        titles = [c['case_metadata']['program_name'] for c in cases]
        self.assertIn('스물다섯 스물하나', titles)
        self.assertIn('메디컬 빅 데이터', titles)
        # 띄어 쓴 이름의 줄바꿈 자리를 붙였으므로 원문 확인 대상이 된다.
        self.assertEqual(result['validation']['status'], 'review_required')
        reasons = [reason for issue in result['validation']['review_issues'] for reason in issue['reasons']]
        self.assertIn('이름 줄바꿈 자리의 띄어쓰기 확인 필요: 스물다섯 스물하나', reasons)
        result2 = m.run(ROOT / 'data/2022_07_specialty_ntt18146.pdf')
        self.assertEqual([c['case_id'] for c in cases], [c['case_id'] for c in result2['cases']])

    def test_no_violation_and_penalty_amount(self):
        cases = self.results['2022_07_specialty_ntt18146.pdf']['cases']
        clear = [c for c in cases if c['case_decision']['result'] == ['문제없음']]
        self.assertTrue(clear)
        self.assertTrue(all(c['case_decision']['provisions'] is None for c in clear))
        self.assertTrue(any(c['case_decision']['result'] == ['과징금 (일천만원)'] for c in cases))

    def test_multi_page_connections_and_row_duplicate_detection(self):
        result = copy.deepcopy(self.results['2026_09_comprehensive_news_ntt21751.pdf'])
        self.assertEqual(result['validation']['counts']['multi_page_cases'], 2)
        result['trace']['joined']['groups'][0]['rows'].append(result['trace']['joined']['groups'][0]['rows'][0])
        self.assertEqual(m.validate_extraction(result['trace'], result['cases'])['status'], 'failed')

    def test_missing_keys_wrong_types_and_dates(self):
        for change in [lambda c:c.pop('case_id'), lambda c:c.update(case_date='2026-02-30'),
                       lambda c:c['case_metadata'].update(broadcast_time=None),
                       lambda c:c['case_metadata']['broadcast_time'][0].update(start_time='25:00'),
                       lambda c:c['source'].update(page=[0])]:
            c = copy.deepcopy(self.sample()['cases'][0])
            change(c)
            self.assertTrue(m.validate_cases([c]))
        c = copy.deepcopy(self.sample()['cases'][0])
        c['case_metadata']['broadcast_time'][0]['end_time'] = '24:00'
        self.assertFalse(m.validate_cases([c]))

    def test_content_differing_from_raw_cells_fails_validation(self):
        result = self.sample()
        result['cases'][0]['case_decision']['content'] = '누락'
        self.assertEqual(m.validate_extraction(result['trace'], result['cases'])['status'], 'failed')

    def test_save_writes_only_the_case_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'cases.json'
            result = self.sample()
            m.save_json(result, output)
            before = output.read_bytes()
            self.assertNotEqual(result['validation']['status'], 'failed')
            self.assertEqual(json.loads(output.read_text()), result['cases'])
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['cases.json'])
            m.save_json(result, output)
            self.assertEqual(output.read_bytes(), before)

    def test_output_conflict_and_failed_extraction_leave_existing_file_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'case.json'
            output.write_text('original')
            with self.assertRaises(FileExistsError):
                m.save_json(self.sample(), output)
            result = self.sample()
            result['cases'][0].pop('case_id')
            with self.assertRaises(ValueError):
                m.save_json(result, output, overwrite=True)
            self.assertEqual(output.read_text(), 'original')
            new_output = Path(tmp) / 'new.json'
            with self.assertRaises(ValueError):
                m.save_json(result, new_output)
            self.assertFalse(new_output.exists())
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['case.json'])

    def test_final_file_rejects_envelope_duplicates_and_mixed_sources(self):
        cases = self.sample()['cases']
        self.assertTrue(m.validate_output({'cases': cases}))
        self.assertTrue(m.validate_output(cases + [copy.deepcopy(cases[0])]))
        cases[0]['source']['file'] = 'other.pdf'
        self.assertTrue(m.validate_output(cases))

    def test_core_ambiguity_stays_review_required(self):
        result = self.sample()
        result['trace']['normalized'][0]['warnings'].append('날짜 소속 확인 필요')
        self.assertEqual(m.validate_extraction(result['trace'], result['cases'])['status'], 'review_required')


if __name__ == '__main__':
    unittest.main()
