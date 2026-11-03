# [3] public_data

공개 데이터는 **원본을 저장소에 넣지 않고** `registry.yaml`에 목록·출처·라이선스만 기록한다.
원본은 `data/raw/public/<id>/`(gitignore)나 연구실 서버에 둔다.

새 데이터셋 추가 절차:
1. `registry.yaml`에 항목 추가 (필수: `id`, `title`, `source_url`, `license`)
2. `uv run pytest modules/public_data`로 형식 검증
3. PR 제목: `[public-data] <데이터셋 이름> 추가`

TODO: 데이터셋별 다운로더, 스키마(컬럼) 자동 요약, 갱신 주기 점검
