"""관련 조항 원문 → 조항 ID 변환을 실제 규정 JSON으로 검증한다."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import provision_rules as rules

REGULATIONS = ROOT.parent / 'regulation_db' / 'output' / 'regulations'


class ProvisionRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.regulations = rules.Regulations(REGULATIONS)

    def resolve(self, text):
        return self.regulations.resolve(text)

    def test_basic_and_spacing_noise(self):
        gold, errors, _ = self.resolve('「상품소개 및 판매방송 심의에 관한 규정」제40 조(가격표시)제3항제3호')
        self.assertEqual((gold, errors), (['SALES_REVIEW_ART40_P3_I3'], []))

    def test_several_regulations_with_and_without_brackets(self):
        gold, errors, _ = self.resolve('방송심의에 관한 규정 제14조, 「방송프로그램의 등급분류 및 표시 등에 관한 규칙」제4조제1항')
        self.assertEqual((gold, errors), (['BR_REVIEW_ART14', 'RATING_RULE_ART4_P1'], []))
        gold, errors, _ = self.resolve('「방송심의에 관한 규정」 제27조, 방송광고심의에 관한 규정 제10조')
        self.assertEqual(gold, ['BR_REVIEW_ART27', 'AD_REVIEW_ART10'])

    def test_unknown_law_is_reported_not_attached_to_previous_regulation(self):
        for text in ('「방송심의에 관한 규정」제33조제1항, 방송법 제5조제1항', '「방송심의에 관한 규정」제33조제1항 및 방송법 제5조'):
            with self.subTest(text=text):
                gold, errors, _ = self.resolve(text)
                self.assertEqual(gold, ['BR_REVIEW_ART33_P1'])
                self.assertEqual(errors, ['대상 규정이 아님: 방송법'])
        self.assertEqual(self.resolve('「방송심의에 관한 규정」제14조 / 방송광고심의에 관한 규정 제10조')[0],
                         ['BR_REVIEW_ART14', 'AD_REVIEW_ART10'])
        gold, errors, _ = self.resolve('「방송심의에 관한 규정」제14조 외 국가법제5조')
        self.assertEqual(gold, ['BR_REVIEW_ART14'])
        self.assertTrue(errors[0].startswith('규정명을 확인할 수 없음'))

    def test_branch_number_in_both_spellings(self):
        for text in ('「방송광고심의에 관한 규정」제33조제1항제4의2호', '「방송광고심의에 관한 규정」제33조제1항제4호의2'):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text)[0], ['AD_REVIEW_ART33_P1_I4_2'])
        self.assertEqual(self.resolve('「방송심의에 관한 규정」제38조의2제1항')[0], ['BR_REVIEW_ART38_2_P1'])

    def test_subitems_including_joined_letters(self):
        for text in ('「방송광고심의에 관한 규정」제43조의2제1항제1호가·나목', '「방송광고심의에 관한 규정」제43조의2제1항제1호가.나목'):
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text), (['AD_REVIEW_ART43_2_P1_I1_A', 'AD_REVIEW_ART43_2_P1_I1_B'], [], []))
        self.assertEqual(rules.subitem_letter('하'), 'N')
        self.assertEqual(rules.subitem_letter('거'), 'O')

    def test_range_is_reported_and_other_parts_are_kept(self):
        gold, errors, _ = self.resolve('「방송심의에 관한 규정」제14조, 제16조제1항부터 제3항까지')
        self.assertEqual(gold, ['BR_REVIEW_ART14'])
        self.assertTrue(errors and errors[0].startswith('범위 인용'))

    def test_law_name_inside_explanation_brackets_is_ignored(self):
        gold, errors, _ = self.resolve('「방송광고심의에 관한 규정」제33조제1항(「식품표시광고법」 위반)')
        self.assertEqual((gold, errors), (['AD_REVIEW_ART33_P1'], []))

    def test_article_heading_fallback_and_mismatch_notes(self):
        gold, errors, notes = self.resolve('「방송심의에 관한 규정」 제58조의2(방송사고)')
        self.assertEqual((gold, errors), (['BR_REVIEW_ART55_2'], []))
        self.assertTrue(notes[0].startswith('조 번호 보정'))
        gold, errors, notes = self.resolve('「방송광고심의에 관한 규정」제25조(식품등)제1항')
        self.assertEqual(gold, ['AD_REVIEW_ART25_P1'])
        self.assertTrue(notes[0].startswith('조 제목 다름'))
        self.assertEqual(self.resolve('「방송심의에 관한 규정」제44조(어린이 청소년 시청자보호)')[2], [])   # 문장부호 차이는 같은 제목

    def test_missing_deleted_and_empty_provisions_are_errors(self):
        self.assertEqual(self.resolve('「방송광고심의에 관한 규정」제18조(진실성)제6항')[0], [])
        gold, errors, _ = self.resolve('「방송심의에 관한 규정」제58조')
        self.assertEqual(gold, [])
        self.assertTrue(errors[0].startswith('삭제된 조항'))
        self.assertEqual(self.resolve(None), ([], ['관련조항 없음'], []))
        self.assertEqual(self.resolve('제14조')[1], ['규정명이 명시되지 않음'])

    def test_paragraph_json_folder_is_rejected(self):
        with self.assertRaises(SystemExit):
            rules.Regulations(ROOT.parent / 'regulation_db' / 'output' / 'paragraphs')


if __name__ == '__main__':
    unittest.main()
