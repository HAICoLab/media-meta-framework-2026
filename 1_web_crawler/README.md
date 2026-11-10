# 1. 웹 페이지 메타데이터 추출

웹 사이트 주소를 입력받아 해당 페이지를 크롤링하고 메타데이터를 추출합니다.

## 구성

| 경로 | 내용 |
|---|---|
| `core/` | 프로젝트 경로, SQLite DB 레이어, Playwright 브라우저, 진행상황 추적 |
| `crawlers/` | 공식 사이트·MBC충북·com2usN·YouTube 크롤러, 메타데이터 조립 |
| `wiki/` | 위키피디아 문서 파싱·선택, 위키 덤프 인덱싱 |
| `services/` | CJ ENM(tvN·TVING) 수집 결과 동기화, 위키 덤프 갱신, 로그 버퍼 |
| `requirements.txt` | 의존성 (이 폴더와 `4_framework` 공용) |
| `THIRD_PARTY_NOTICES.md` | 서드파티 라이선스 목록 |

DB(`dramas.db`)·위키 덤프·수집 결과(`data/`)는 이 폴더 안에 생성되며 Git 에는 올리지 않습니다.
웹 UI·API 는 [`4_framework`](../4_framework) 에서 이 폴더의 모듈을 불러와 실행합니다.

## 설치

```bash
pip install -r requirements.txt
playwright install chromium
```

CJ ENM(tvN·TVING) 수집은 외부 모듈 `broadcast_web_crawler` 를 사용하며, 소스 경로는 환경변수 `BWC_SRC` 로 지정합니다.
