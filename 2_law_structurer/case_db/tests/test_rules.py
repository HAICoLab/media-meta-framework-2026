"""실제 PDF에서 관찰한 헤더·날짜·이름·결과 표현을 검사한다."""
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from case_db import case_rules as r


class RuleChecks(unittest.TestCase):
    def group(self, headers, values):
        h = r.identify_header(headers)
        return {'case_id': '2026-방송-21-0130', 'warnings': [], 'generated_id': False,
                'fragments': [{'handler': k, 'value': v} for k, v in zip(h['handlers'], values)]}

    def test_split_number_inside_digits(self):
        self.assertEqual(r.case_ids('2026-09-08\n2026-방송-21-0\n130\n보도교양'), ['2026-방송-21-0130'])

    def test_date_is_not_case_id(self):
        self.assertEqual(r.case_ids('2026-09-08'), [])

    def test_shuffled_headers_and_composite_cells(self):
        p = r.split_cells(self.group(['심의결과', '관련조항', '프로그램명(방송일시)', '의결번호', '의결일', '의결 내용', '방송사'],
                                     ['권고,등급조정 요구', '제14조', '프로그램 A (2026.1.1.목,10:00~11:00)', '2026-방송-21-0130', '2026-09-08', '본문', 'MBC']))
        self.assertEqual((p['program_name'], p['case_date']), ('프로그램 A', '2026-09-08'))
        self.assertEqual(p['result'], ['권고', '등급조정 요구'])
        self.assertEqual(p['broadcast_time'][0]['date'], '2026-01-01')

    def test_old_header_alias(self):
        h = r.identify_header(['월', '의결일', '의결번/심의\n부문', '방송사', '프로그램명', '방송일시', '문제방송내용', '관련조항', '심의의결\n내용'])
        self.assertEqual(h['handlers'][2], 'identity')

    def test_missing_id_header_and_body_keyword(self):
        self.assertIsNotNone(r.identify_header(['순번', '의결일', '방송사', '채널명', '프로그램명\n(방송일시)', '의결 내용', '관련조항', '심의결과']))
        self.assertIsNone(r.identify_header(['MBC', '프로그램명을 잘못 표시한 방송', '권고']))

    def test_multiline_station_with_quoted_title(self):
        self.assertEqual(r.split_party_program("cpbc(가톨릭평화방\n송)-FM\n'김혜영의 뉴스공감'", []),
                         ('cpbc(가톨릭평화방\n송)-FM', "'김혜영의 뉴스공감'"))

    def test_multiple_broadcasts_overnight(self):
        warnings = []
        out = r.parse_broadcast_time('2026.1.1.목,22:00~00:20\n2026.1.2.금,10:00~11:00', warnings)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]['end_time'], '00:20')
        self.assertEqual(warnings, [])

    def test_missing_and_invalid_dates_are_not_invented(self):
        warnings = []
        out = r.parse_broadcast_time('19:40~21:00\n2024.08.07.수,19:30~20:20', warnings)
        self.assertIsNone(out[0]['date'])
        self.assertTrue(warnings)
        out = r.parse_broadcast_time('2026.2.30. 25:00~26:00', warnings)
        self.assertIsNone(out[0]['date'])
        self.assertIsNone(out[0]['start_time'])

    def test_name_evidence_and_conflicting_spellings(self):
        refs = r.make_name_references(['오한진의 백세알부민을 소개했다'])
        value, notes, ambiguous = r.normalize_name('오한진의 백세\n알부민', 'program_name', refs)
        self.assertEqual(value, '오한진의 백세알부민')
        self.assertFalse(ambiguous)
        refs = r.make_name_references(['오한진의 백세알부민', '오한진의 백세 알부민'])
        self.assertTrue(r.normalize_name('오한진의 백세\n알부민', 'program_name', refs)[2])

    def test_unknown_names_and_duration(self):
        self.assertEqual(r.normalize_name('처음보는\n이름', 'program_name'), ('처음보는이름', [
            {'method': 'final_name', 'before': '처음보는\n이름', 'after': '처음보는이름'}], False))
        # 띄어 쓴 이름에서 한글 사이를 붙이면 애매한 것으로 표시한다.
        self.assertEqual(r.normalize_name('이상한 과자가게\n전천당', 'program_name')[::2],
                         ('이상한 과자가게전천당', True))
        self.assertEqual(r.normalize_name('채널\nTV', 'channel')[0], '채널 TV')
        self.assertEqual(r.normalize_name('파워솔루션(5\n분 30초)', 'program_name')[0], '파워솔루션(5분 30초)')
        self.assertEqual(r.normalize_name('광고명\n(15초)', 'program_name')[0], '광고명 (15초)')
        self.assertEqual(r.normalize_name('광고명(15\n초)', 'program_name')[0], '광고명(15초)')
        self.assertEqual(r.normalize_name('FEBC(극동방송)\n-표준FM', 'broadcaster')[0], 'FEBC(극동방송)-표준FM')

    def test_observed_latin_token_and_durationless_reference(self):
        refs = r.make_name_references(['JTBC 뉴스룸'])
        self.assertEqual(r.normalize_name('올림픽특집JTB\nC뉴스룸', 'program_name', refs)[0], '올림픽특집JTBC뉴스룸')
        self.assertEqual(r.normalize_name('MBC\nSPORTS+', 'channel', refs)[0], 'MBC SPORTS+')
        refs = r.make_name_references(['김봉찬 명인의 약선 연잎밥 판매방송에서'])
        self.assertEqual(r.normalize_name('김봉찬 명인의 약선 연\n잎밥(8분)', 'program_name', refs)[0], '김봉찬 명인의 약선 연잎밥(8분)')

    def test_result_linebreaks_do_not_create_fake_results(self):
        self.assertEqual(r.parse_result('권고,등급\n조정요구'), ['권고', '등급조정 요구'])
        self.assertEqual(r.parse_result('과징금\n(일천만원)'), ['과징금 (일천만원)'])
        self.assertEqual(r.parse_result('권고\n등급조정 요구'), ['권고', '등급조정 요구'])


if __name__ == '__main__':
    unittest.main()
