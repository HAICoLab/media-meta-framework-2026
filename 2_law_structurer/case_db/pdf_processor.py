"""pdfplumber로 페이지별 표·셀·좌표를 추출한다. 값의 의미 해석은 case_rules.py가 담당한다."""
from pathlib import Path

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException


def extract_tables(path, source):
    """PDF의 페이지별 표를 행·셀 문자열과 셀 좌표로 추출한다. 읽을 수 없는 PDF는 ValueError를 낸다."""
    try:
        with pdfplumber.open(path) as pdf:
            pages = [_extract_page(number, page) for number, page in enumerate(pdf.pages, 1)]
    except PdfminerException as exc:
        raise ValueError(f'PDF를 읽을 수 없습니다: {Path(path).name}') from exc
    return {'source': source, 'pages': pages}


def _extract_page(number, page):
    # 겹쳐 그려진 같은 글자를 하나로 합친다.
    working = page.dedupe_chars()
    verticals = [e for e in working.edges if e['orientation'] == 'v' and e['bottom'] - e['top'] > 30]
    # 표가 페이지를 넘어가며 위·아래 테두리가 빠진 경우를 위해, 세로선이 실제로 있는 범위의
    # 위·아래 끝에만 가로선을 더한다.
    settings = {'explicit_horizontal_lines': [min(e['top'] for e in verticals),
                                              max(e['bottom'] for e in verticals)]} if verticals else {}
    tables = []
    for ti, table in enumerate(working.find_tables(settings), 1):
        rows = table.extract()
        column_boxes = []
        for ci in range(max(map(len, rows), default=0)):
            boxes = [r.cells[ci] for r in table.rows if ci < len(r.cells) and r.cells[ci]]
            # 첫 열에는 표 전체 너비의 병합 제목 셀이 있을 수 있으므로 가장 좁은 셀을 열 폭으로 쓴다.
            cell = min(boxes, key=lambda b: b[2] - b[0])
            column_boxes.append((cell[0], cell[2]))
        # 열 영역만 잘라 따로 읽는다. 줄바꿈된 의결번호를 이웃 열 글자 없이 얻기 위해서다.
        top, bottom = table.bbox[1], table.bbox[3]
        column_texts = [working.crop((x0, top, x1, bottom)).extract_text() or ''
                        for x0, x1 in column_boxes]
        tables.append({'table': ti, 'rows': rows, 'column_texts': column_texts,
                       'cell_bboxes': [[list(c) if c else None for c in row.cells] for row in table.rows]})
    return {'page': number, 'width': page.width, 'tables': tables,
            'duplicate_chars_removed': len(page.chars) - len(working.chars)}
