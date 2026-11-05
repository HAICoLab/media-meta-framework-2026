"""줄바꿈·병합·생략된 테두리 등 TXT에서 실제 확인한 오류 패턴."""
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import table_processor as tables
import make_json_regulations as reg


class Tables(unittest.TestCase):
    def test_cell_breaks_pipe_and_html_are_safe(self):
        text = tables.render_markdown([['항목', '내용'], ['A|B', '첫째\n둘째 <script>']])
        self.assertIn(r'A\|B', text)
        self.assertIn('첫째<br>둘째 &lt;script&gt;', text)
        tables.validate_markdown(text, 2)
        self.assertEqual(len(text.splitlines()), 3)

    def test_data_without_header_gets_empty_header(self):
        text = tables.render_markdown([['부산', '0.6172%'], ['대구', '0.4534%']])
        self.assertEqual(text.splitlines()[0], '|  |  |')
        self.assertIn('| 부산 | 0.6172% |', text)

    def test_header_width_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            tables.render_markdown([['A', 'B'], ['C']])
        with self.assertRaises(ValueError):
            tables.validate_markdown('| A | B |\n| --- | --- | --- |', 2)

    def test_blank_form_rows_are_kept(self):
        text, warnings = tables.process_tables('┌──┬──┐\n│이름│주소│\n├──┼──┤\n│  │  │\n└──┴──┘')
        self.assertFalse(warnings)
        self.assertEqual(text.splitlines()[-1], '|  |  |')

    def test_wrapped_text_is_joined_inside_same_cell(self):
        text, warnings = tables.process_tables('┌──┬──┐\n│구분│내용│\n├──┼──┤\n│A│문장의│\n│ │계속│\n└──┴──┘')
        self.assertFalse(warnings)
        self.assertIn('| A | 문장의 계속 |', text)

    def test_repeated_percent_rows_do_not_merge_values(self):
        text, warnings = tables.process_tables('┌──┬──┐\n│부산│0.6172%│\n│대구│0.4534%│\n└──┴──┘')
        self.assertFalse(warnings)
        self.assertIn('| 부산 | 0.6172% |\n| 대구 | 0.4534% |', text)

    def test_no_issue_leaves_prose_unchanged(self):
        original = '표와 무관한 문장\nhttps://example.org/source\n1. 항목'
        self.assertEqual(tables.process_tables(original), (original, []))

    def test_missing_boundary_preserves_text_with_warning(self):
        original = '┌──┬──┐\n│A│B│'
        text, warnings = tables.process_tables(original)
        self.assertTrue(warnings)
        self.assertIn(original, text)
        self.assertTrue(text.startswith('```text'))

    def test_open_edges_and_title_on_same_line(self):
        for original in ['──┬──\n이름│주소\n──┴──', '제목┏━━┯━━┓\n┃이름│주소┃\n┗━━┷━━┛']:
            text, warnings = tables.process_tables(original)
            self.assertFalse(warnings)
            self.assertIn('| 이름 | 주소 |', text)

    def test_all_real_tables_have_no_unresolved_grid(self):
        count = 0
        for path in (ROOT / 'data').glob('*.txt'):
            result = reg.run(path)
            self.assertFalse([r for r in result['validation']['review_issues'] if '표 원문 보존' in r['reason']], path.name)
            for record in result['records']:
                if record['region'] == 'body':
                    continue
                self.assertFalse(re.search(r'[\u2500-\u257f]', record['text']), path.name)
                self.assertNotIn('```text', record['text'])
                count += len(re.findall(r'^\| (?:--- \| ?)+$', record['text'], re.M))
        self.assertEqual(count, 241)

    def test_monthly_merged_headers_and_rate_totals(self):
        result = reg.run(ROOT / 'data/방송광고_결합판매_지원고시.txt')
        text = '\n'.join(r['text'] for r in result['records'] if r['region'] == 'appendix')
        self.assertIn('12월', text)
        self.assertIn('0.6172%', text)
        self.assertIn('0.0170%', text)
        self.assertIn('12.0461%', text)
        form = next(r for r in result['records'] if r['label'] == '별지1')
        self.assertEqual(form['text'].count('1사분기'), 1)

    def grids(self, text):
        blocks = re.findall(r'(?m)^\|.*(?:\n\|.*)*', text)
        return [[re.split(r'(?<!\\)\|', line)[1:-1] for line in block.splitlines()]
                for block in blocks]

    def form(self, filename, label):
        result = reg.run(ROOT / 'data' / (filename + '.txt'))
        return next(r['text'] for r in result['records'] if r['region'] == 'appendix' and r['label'] == label)

    def test_full_boundary_excludes_vertical_merge_and_text(self):
        self.assertTrue(tables._full_grid_boundary('├──┬──┤', 7))
        for line in ['│  ├──┤', '│청├──┤', '├──┼─┤']:
            self.assertFalse(tables._full_grid_boundary(line, 7), line)

    def test_broadcast_form_uses_local_widths_and_keeps_input_cells(self):
        text = self.form('방송프로그램등의_편성에_관한_고시', '별지제1호서식')
        grids = self.grids(text)
        self.assertEqual([len(g[0]) for g in grids], [3, 5, 2, 4, 5, 4, 2, 5])
        self.assertEqual([v.strip() for v in grids[1][2]], ['신 청 인', '①대 표 자', '', '②생 년 월 일', ''])
        self.assertEqual([v.strip() for v in grids[2][2]], ['⑦제 목', ''])
        self.assertNotIn('접수정보', text)
        self.assertNotIn('신청인 정보', text)
        self.assertIn('?편 집 책 임 자', text)

    def test_partial_boundary_keeps_cast_rows_and_shared_label_together(self):
        text = self.form('영화_및_비디오물의_진흥에_관한_법률_시행규칙', '별지제5호서식')
        grids = self.grids(text)
        cast = next(g for g in grids if any('출연자' in cell for row in g for cell in row))
        self.assertEqual(len(cast), 4)  # 빈 머리행·구분행·주연·조연
        self.assertEqual([v.strip() for v in cast[2]], ['출연자 (국적)', '주연', '( )', '기타 중요역할자 (국적)', '( )'])
        self.assertEqual([v.strip() for v in cast[3]], ['', '조연', '( )', '', '( )'])

    def test_original_empty_receipt_rows_remain_four(self):
        text = self.form('영화_및_비디오물의_진흥에_관한_법률_시행규칙', '별지제1호서식')
        grid = next(g for g in self.grids(text) if any('관람 인원' in c for c in g[0]))
        self.assertEqual(len(grid[0]), 5)
        self.assertEqual(sum(all(not cell.strip() for cell in row) for row in grid[2:]), 4)
        self.assertEqual(grid[-1][0].strip(), '합계')

    def test_multilevel_header_is_not_detached_from_monthly_rows(self):
        text = self.form('방송광고_결합판매_지원고시', '별지1')
        grids = self.grids(text)
        self.assertEqual(len(grids), 1)
        self.assertEqual(len(grids[0][0]), 14)
        self.assertIn('1사분기', text)
        self.assertIn('12월', text)

    def test_split_is_rejected_when_extra_headers_increase_cells(self):
        text = self.form('방송프로그램등의_편성에_관한_고시', '별지제5호서식')
        grids = self.grids(text)
        self.assertEqual(len(grids), 2)  # 작품명 표 + 작성자 5명의 반복 표
        self.assertEqual(len(grids[1][0]), 6)
        self.assertEqual(len(grids[1][2:]), 10)
        self.assertEqual(text.count('(서명 또는 인)'), 5)

    def test_new_section_does_not_turn_first_field_into_column_title(self):
        text = self.form('영화_및_비디오물의_진흥에_관한_법률_시행규칙', '별지제5호서식')
        grid = next(g for g in self.grids(text) if any('촬영지, 촬영장비' in c for row in g for c in row))
        self.assertTrue(all(not cell.strip() for cell in grid[0]))
        self.assertEqual(grid[2][0].strip(), '촬영지, 촬영장비ㆍ시설 내역')


if __name__ == '__main__':
    unittest.main()
