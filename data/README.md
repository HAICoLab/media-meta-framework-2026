# data/

이 폴더의 내용은 `README.md`를 빼고 모두 git에서 제외됩니다.

```
data/
├── raw/web/         # 크롤링 원본 HTML
├── raw/law/         # 법령 원문 TXT
├── raw/public/<id>/ # 공개 데이터 원본
└── processed/       # 추출된 메타데이터 (JSONL)
```

공유가 필요한 데이터는 연구실 서버나 Hugging Face Datasets(private)에 올리고 위치를 여기에 적어 주세요.
테스트에 쓰는 작은 샘플은 각 모듈의 `tests/fixtures/`에 둡니다.
