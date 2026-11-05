# 2. 법령 데이터 구조화

방송·미디어 규정 원문과 심의의결 사례를 구조화된 JSON으로 변환합니다. 서로 독립된 두 처리기로 구성됩니다.

- **규정 DB (`regulation_db`)**: 규정 TXT → 조·항·호·목 계층 JSON, 항 단위 검색용 JSON
- **심의사례 DB (`case_db`)**: 심의의결 현황 PDF → 사례 단위 JSON

두 처리기 모두 명령줄에서 실행하며, 입력을 변환·검증한 뒤 JSON으로 저장합니다.

## 디렉터리 구성

두 처리기는 모듈을 같은 방식으로 나눕니다.

- **[흐름]**: 입출력, 처리 순서, 검증, 저장
- **[표·셀]**: 원본의 표 구조 읽기
- **[규칙]**: 문서 형식에 따른 해석

```text
2_law_structurer/
├── README.md
├── requirements.txt                # 외부 의존성 (case_db: pdfplumber)
│
├── regulation_db/                  # 규정 TXT → 규정 JSON
│   ├── make_json_regulations.py    # [흐름]  TXT 읽기 → 계층 구성 → 검증 → 규정 JSON 저장
│   ├── make_json_paragraphs.py     # [흐름]  규정 JSON → 항 검색 JSON 저장
│   ├── table_processor.py          # [표·셀] 괘선 문자로 그린 표 해석 → Markdown 표
│   ├── document_rules/             # [규칙]  입력 형식별 해석 규칙
│   │   ├── common.py               #         공통: 본문·부칙·별표 영역, 조·항·호·목 번호 식별
│   │   ├── administrative_rule.py  #         행정규칙 TXT: 수집 머리말·API 접두부 분리
│   │   └── law_rule.py             #         법령 TXT: 반복되는 API 조문 정보·중복 번호 정리
│   ├── tests/
│   ├── data/                       # 입력: 규정 TXT
│   └── output/
│       ├── regulations/            # 출력: 규정 JSON
│       └── paragraphs/             # 출력: 항 검색 JSON
│
└── case_db/                        # 심의의결 PDF → 사례 JSON
    ├── make_json_cases.py          # [흐름]  PDF 읽기 → 사례 구성 → 검증 → 사례 JSON 저장
    ├── pdf_processor.py            # [표·셀] PDF의 표·셀·좌표 추출
    ├── case_rules.py               # [규칙]  헤더 별칭, 복합 셀 분리, 날짜·이름·결과 정규화
    ├── tests/
    ├── data/                       # 입력: 심의의결 PDF, 출처 목록 sources.json
    └── output/
        └── cases/                  # 출력: 사례 JSON
```

새 입력 형식은 [규칙] 모듈을 추가해 지원합니다. 규정 DB의 규칙 모듈은 `preprocess`, `split_sections`, `heading`, `identify_nodes` 함수를 제공해야 하며, 적용할 모듈은 `make_json_regulations.select_rules()`에서 선택합니다.

## 설치

Python 3.13에서 테스트했습니다. 규정 DB는 표준 라이브러리만 사용하고, 심의사례 DB는 `pdfplumber`가 필요합니다.

```bash
cd 2_law_structurer
python -m pip install -r requirements.txt
```

## 사용법

### 규정 DB

```bash
cd 2_law_structurer/regulation_db

# 1단계: TXT → 규정 JSON
python make_json_regulations.py data output/regulations

# 2단계: 규정 JSON → 항 검색 JSON
python make_json_paragraphs.py output/regulations output/paragraphs
```

- 입력에는 파일 하나 또는 폴더를 지정합니다.
- 두 단계는 따로 실행할 수 있습니다. 기존 규정 JSON에서 항 검색 JSON만 다시 만들 수도 있습니다.
- 2단계에서 입력과 출력 경로가 같으면 저장하지 않습니다.

### 심의사례 DB

```bash
cd 2_law_structurer/case_db

# PDF → 사례 JSON
python make_json_cases.py data output/cases

# 같은 처리를 하면서 특정 사례의 단계별 처리 과정(원시 셀·값 분리·최종 값)을 함께 출력
python make_json_cases.py data output/cases --case-id 2026-광고-04-0045
```

- 입력에는 PDF 하나 또는 폴더를 지정합니다.
- `data/sources.json`이 있으면 게시물 URL, 보고 기간 등 PDF에 없는 출처 정보를 함께 씁니다. 없으면 해당 값은 `null`이 됩니다.

### 기존 결과가 있을 때

세 명령 모두 출력 폴더에 같은 이름의 파일이 있으면 다음과 같이 동작합니다.

| 경우 | 동작 |
|---|---|
| 새 결과와 내용이 같음 | 기존 파일을 그대로 둡니다 |
| 새 결과와 내용이 다름 | 저장하지 않고 `failed`로 표시합니다. 기존 파일은 유지됩니다 |
| `--overwrite` 지정 | 내용이 다른 파일을 새 결과로 교체합니다 |

### 실행 결과

입력 파일마다 처리 결과가 JSON 한 줄로 출력됩니다. `failed`인 파일이 하나라도 있으면 종료 코드는 1입니다.

```text
{"file": "2024_02_specialty_ntt19732.pdf", "status": "review_required", "errors": [], "review_issues": [{"case_id": "2024-방송-01-0008", "pages": [1], "reasons": ["이름 줄바꿈 자리의 띄어쓰기 확인 필요: 어서와 한국은 처음이지?"]}], "counts": {"pages": 2, "cases": 2, ...}}
```

| `status` | 의미 | 저장 |
|---|---|---|
| `passed` | 검증 통과 | O |
| `review_required` | 저장했지만 원문 확인이 필요한 항목이 있음 (`review_issues` 참고) | O |
| `failed` | 검증 실패(원문 누락·변경 등) 또는 기존 결과와 내용이 다름 | X (기존 파일 유지) |

처리 상태와 검토 사유는 화면에만 출력되고 결과 JSON에는 들어가지 않습니다. 기록이 필요하면 출력을 파일로 저장합니다.

```bash
python make_json_cases.py data output/cases > run_log.jsonl
```

## 처리 방식

### 규정 DB

1. TXT 머리말의 출처로 입력 형식을 판별합니다. `(target=admrul)` 또는 `행정규칙집 수록`이면 행정규칙, `(target=law)`이면 법령 규칙을 적용하고, 판별할 수 없으면 오류로 처리합니다.
2. 본문·부칙·별표(별지·서식 포함) 영역을 나누고, 본문은 조·항·호·목 계층으로 구성합니다. 상위 편·장·절 제목은 `parents`에 기록합니다.
3. 괘선으로 그린 표는 Markdown 표로 변환합니다. 한 셀 안의 여러 줄은 공백으로 잇고, 병합 셀의 내용은 왼쪽 위 셀에 한 번만 씁니다. 구조가 불분명한 표는 원문을 코드 블록으로 보존하고 검토 사유를 남깁니다.
4. 개정문, 별표 다운로드 URL 등 수집 과정에서 붙은 부가 정보는 제외합니다.
5. 변환 결과를 원문과 대조해 문구·번호·순서가 빠지거나 바뀌지 않았는지 검사합니다.

### 심의사례 DB

1. PDF 페이지별로 표·셀·좌표를 추출합니다. 의결번호 열은 그 열 영역만 따로 읽어 셀 추출 결과와 대조합니다.
2. 헤더 표기 차이(예: `의결내용`/`논의내용`/`문제방송내용`)를 공통 필드로 대응시키고, 페이지를 넘어 이어지는 사례를 연결합니다.
3. `방송사/프로그램명`처럼 한 셀에 여러 값이 있으면 분리하고, 날짜·시각·이름·심의결과를 정규화합니다.
4. 셀 안에서 줄이 바뀐 이름은 다음 순서로 정리합니다.
   - 다른 셀에 같은 이름이 한 줄로 적힌 곳이 하나뿐이면 그 표기를 따릅니다.
   - 근거가 없으면 한글과 한글 사이의 줄바꿈은 붙이고(`(주)씨제이⏎이엔엠` → `(주)씨제이이엔엠`), 나머지 줄바꿈은 한 칸 띄웁니다.
   - 띄어 쓴 이름에서 한글 사이를 붙였다면 원래 띄어쓰기였을 수 있으므로 `review_required`로 표시합니다.
5. 최종 값을 원시 셀과 다시 대조해 글자가 빠지거나 더해지거나 순서가 바뀌지 않았는지 검사합니다.

## 출력 형식

### 규정 JSON (`regulation_db/output/regulations/`)

TXT 하나당 JSON 파일 하나이며, 본문 조문·부칙·별표 레코드를 하나의 리스트에 담습니다.

```json
{
  "document_id": "2100000279340",
  "document_name": "불법촬영물등 유통방지를 위한 기술적·관리적 조치 기준",
  "source": {"file": "불법촬영물등_유통방지를_위한_기술적관리적_조치_기준.txt", "effective_date": "2026-05-18"},
  "region": "body",
  "region_label": "본문",
  "type": "article",
  "label": "제11조(성능평가 기준)",
  "text": "제11조(성능평가 기준)",
  "children": [
    {
      "type": "paragraph",
      "label": "제1항",
      "text": "① 성능평가 시 평가지표는 다음 각 호와 같다.",
      "children": [
        {"type": "item", "label": "제1호", "text": "1. 식별 가능성 : ...", "children": []}
      ]
    }
  ]
}
```

| 필드 | 내용 |
|---|---|
| `document_id` | 행정규칙 일련번호 또는 법령마스터 ID |
| `document_name` | 문서명 |
| `source` | `{file, effective_date}`. 시행일은 `YYYY-MM-DD` 또는 `null` |
| `region` | `body`(본문), `supplement`(부칙), `appendix`(별표·별지·서식) |
| `region_label` | 영역 이름. 부칙은 날짜·공포번호, 별표·별지는 식별 이름 |
| `parents` | 상위 제목이 있는 본문 조문에만 있음. `{part, chapter, section}` 중 해당 항목 |
| `type` | 본문 최상위 `article`, 부칙·별표 `block`, 하위 노드 `paragraph`·`item`·`subitem` |
| `label` | 식별 이름. 예: `제10조(공정성)`, `제1항`, `제1호`, `가목` |
| `text` | 해당 노드의 원문 (번호 포함, 하위 노드 내용 제외) |
| `children` | 하위 노드 리스트 |
| `annotations` | 조문에 별도 주석이 있을 때만 있음. `{source_line, text}` 리스트 |

### 항 검색 JSON (`regulation_db/output/paragraphs/`)

본문 조문을 항 단위로 나눈 JSON입니다. 부칙·별표는 포함하지 않으며, 항이 없는 조는 조 전체를 레코드 하나로 만듭니다.

```json
{
  "article_path": "연속편성 세부기준 등에 관한 고시 제1조(목적)",
  "provision_path": "연속편성 세부기준 등에 관한 고시 제1조(목적)",
  "content": "제1조(목적) 이 고시는 「방송법」(이하 \"법\"이라 한다) 제73조제2항 ... 정함을 목적으로 한다."
}
```

| 필드 | 내용 |
|---|---|
| `article_path` | 문서명, 상위 편·장·절, 조문 이름을 공백으로 이은 경로 |
| `provision_path` | `article_path`에 항 이름을 더한 경로 |
| `content` | 해당 항과 하위 호·목을 합친 원문 |

### 사례 JSON (`case_db/output/cases/`)

PDF 하나당 JSON 파일 하나이며, 사례 리스트를 담습니다.

```json
{
  "case_id": "2026-광고-04-0045",
  "case_date": "2026-06-01",
  "case_metadata": {
    "review_category": null,
    "broadcaster": null,
    "channel": "MBC SPORTS+",
    "program_name": "케어리브 멸균 반창고 (15초)",
    "broadcast_time": [{"date": "2025-04-07", "start_time": "08:59", "end_time": "09:00"}]
  },
  "case_decision": {
    "content": "○ 의약외품 ‘케어리브 멸균 반창고(15초)’ 광고에서 ...",
    "provisions": "「방송광고심의에 관한 규정」 제23조(어린이·청소년)제2항제 1호",
    "result": ["의견제시"]
  },
  "source": {
    "file": "2026_06_broadcast_ad_ntt21394.pdf",
    "report_period": "2026-06",
    "category": "broadcast_ad",
    "page": [1],
    "sha256": "b05ed75879601720a271f2e25c5f20447d79ce6143a3058e6d3106329888cb39"
  }
}
```

| 필드 | 내용 |
|---|---|
| `case_id` | 원문 의결번호. 의결번호가 없는 양식은 `local:<원본 SHA-256>:p<페이지>t<표>r<시작행>` 형식의 내부 ID |
| `case_date` | 의결일 (`YYYY-MM-DD`) |
| `case_metadata` | 심의부문, 방송사, 채널, 프로그램명, 방송일시 리스트 |
| `case_decision` | 의결 본문, 관련 조항 원문, 심의결과 리스트 |
| `source` | 원본 파일명, 보고 기간, 부문, 페이지, 원본 SHA-256 |

- 날짜, 방송사, 채널, 프로그램명, 심의부문, 관련 조항은 `null`일 수 있습니다. `broadcast_time`과 `result`는 항상 리스트입니다.
- 시각은 `HH:MM` 형식이며, `24:00`과 자정을 넘는 방송 시간을 허용합니다.
- `report_period`는 단일 월(`2026-06`) 또는 합본 기간(`2026-07~2026-08`)입니다.
- 게시물·다운로드 URL 등 자세한 출처는 `source.sha256`으로 `data/sources.json`에서 찾습니다.

## 포함된 데이터

| 구분 | 입력 | 출력 |
|---|---|---|
| 규정 | TXT 23개: 방송 관련 행정규칙 20개, 「영화 및 비디오물의 진흥에 관한 법률」·시행령·시행규칙 3개 (국가법령정보센터 Open API) | 규정 JSON 23개(레코드 1,026개), 항 검색 JSON 23개(레코드 1,595개) |
| 심의사례 | 방송미디어통신심의위원회 누리집([www.kocsc.or.kr](https://www.kocsc.or.kr)) 심의의결 현황 PDF 18개(90페이지) | 사례 JSON 18개(사례 203건) |

심의의결 PDF는 전체 기간이 아니라 양식·시기·예외 유형을 확인하기 위해 고른 표본입니다. 2024년 2월(기본 양식), 2026년 6월(양식 변화), 2026년 8~9월(최근 자료)에서 각각 5개 부문을 골랐고, 헤더 표기가 다른 자료, 두 달 합본 자료, 의결번호가 없는 양식을 1개씩 더했습니다.

## 테스트

```bash
(cd 2_law_structurer/regulation_db && python -m unittest discover -s tests)
(cd 2_law_structurer/case_db && python -m unittest discover -s tests)
```

## 참고 사항

- **수동 보정**: 「방송심의에 관한 규정」 제7조의 번호 없는 문장은 원문 확인 결과 제16항에 해당하므로, 저장된 규정 JSON과 항 검색 JSON에서 별도 항으로 분리했습니다. 이 보정은 코드에 없습니다. 그래서 `--overwrite` 없이 1단계를 실행하면 이 파일은 `failed`로 표시되고 보정된 파일이 유지됩니다. `--overwrite`로 다시 만들었다면 두 결과 모두에 보정을 다시 적용해야 합니다.
- **원문 구조 유지**: 삭제된 조항의 빈 번호(「보편적 시청권 관련 금지행위 세부기준」 등)와 호 없이 시작하는 목(「공익채널 선정 및 장애인복지채널 인정에 관한 고시」)은 원문 구조대로 둡니다.
- **PDF별 보정**: 표 경계 때문에 방송일시가 잘못 나뉘는 PDF 1건에는 그 파일의 해시에만 적용되는 보정을 둡니다(`case_rules.py`의 `VERIFIED_BOUNDARY_REPAIRS`).
- **이름 근거 범위**: 이름 줄바꿈의 근거는 함께 처리한 PDF 전체의 셀에서 찾습니다. 따라서 PDF 하나만 처리하면 폴더 전체를 처리했을 때와 이름 띄어쓰기가 다를 수 있습니다. 저장된 결과는 `data/` 폴더 전체를 처리한 것입니다.
- **검증 범위**: 자동 검증은 원문 대비 누락·변경만 확인하며, 전체 필드에 대한 전문가 검수를 대신하지 않습니다. 표본 밖 시기의 PDF 양식에는 규칙 추가가 필요할 수 있습니다.
- **범위 밖**: 사례의 관련 조항과 심의 당시 규정 판본의 연결, DB·검색엔진 적재는 이 폴더에서 다루지 않습니다.
