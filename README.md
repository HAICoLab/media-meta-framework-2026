# media-meta-framework-2026

웹 페이지, 법령 텍스트, 공개 데이터에서 메타데이터를 추출해 **하나의 공통 스키마**(`MetadataRecord`)로 통합하는 프레임워크입니다.

| # | 모듈 | 경로 | 입력 → 출력 |
|---|---|---|---|
| 1 | web_crawler | [`modules/web_crawler`](modules/web_crawler) | URL → 페이지 메타데이터 1건 |
| 2 | law_structurer | [`modules/law_structurer`](modules/law_structurer) | 법령 TXT → 조·항·호·목 계층 레코드 |
| 3 | public_data | [`modules/public_data`](modules/public_data) | `registry.yaml` → 데이터셋 메타데이터 |
| 4 | framework | [`framework`](framework) | 입력 유형 판별 → 모듈 실행 → JSONL |
| – | schema | [`schema`](schema) | 공통 스키마·`BaseExtractor` 인터페이스 |

```
입력(URL / .txt / .yaml) ──► framework.Router ──► 모듈.extract() ──► list[MetadataRecord] ──► JSONL / DB
```

## 시작하기

[uv](https://docs.astral.sh/uv/)가 필요합니다.

```bash
uv sync --all-packages     # 전체 설치
uv run pytest              # 테스트
uv run mmf modules/law_structurer/tests/fixtures/sample_law.txt
uv run mmf https://example.com -o outputs/web.jsonl
```

## 새 모듈 추가

1. `modules/<이름>/`에 패키지를 만들고 `mmf_schema.BaseExtractor`를 상속해 `can_handle`, `extract`를 구현합니다.
2. `framework/src/mmf_framework/router.py`의 `default_router()`에 등록합니다.

협업 규칙은 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.
