# 4. 통합 프레임워크

1, 2, 3번 작업을 하나의 프레임워크로 통합합니다.

## 구성

| 경로 | 내용 |
|---|---|
| `main.py` | FastAPI 앱 엔트리포인트 (`../1_web_crawler` 모듈을 import 경로에 추가) |
| `routers/` | 라이브러리·크롤러·위키 덤프·로그 API |
| `static/index.html` | 웹 UI (콘텐츠 라이브러리, 수동 검색, 크롤러, 로그) |

## 실행

```bash
pip install -r ../1_web_crawler/requirements.txt
playwright install chromium
python main.py
```
