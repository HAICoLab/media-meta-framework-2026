# 협업 규칙

## 브랜치
- `main`은 보호 브랜치입니다. 직접 push하지 않고 PR로만 병합합니다.
- 브랜치 이름: `<모듈>/<종류>-<설명>`
  - 모듈: `crawler`, `law`, `public-data`, `framework`, `schema`, `chore`
  - 예: `crawler/feat-robots-txt`, `law/fix-article-suffix`, `schema/add-license-field`
- 하나의 PR에는 하나의 모듈만 담는 것을 원칙으로 합니다. 예외는 `schema` 변경과 그에 따른 수정입니다.

## 작업 흐름
1. 이슈를 만들거나 배정받습니다 (이슈 템플릿 사용).
2. `main`에서 브랜치를 만듭니다.
3. 커밋 메시지: `[law] 조의2 패턴 처리` 처럼 모듈 태그로 시작합니다.
4. PR을 열고 `Closes #이슈번호`를 적습니다. CI 통과와 리뷰 1건 이후 **Squash merge** 합니다.

## 스키마 변경
`schema/`는 모든 모듈의 약속입니다. 변경하려면
1. `schema` 라벨 이슈로 먼저 논의하고,
2. `SCHEMA_VERSION`을 올리고 `docs/schema-changelog.md`에 기록하며,
3. 영향을 받는 모듈 테스트까지 같은 PR에서 고칩니다.

## 데이터
- 원본 데이터(크롤링 HTML, 데이터셋 원본)는 커밋하지 않습니다. `data/README.md` 참고.
- 공개 데이터는 `modules/public_data/registry.yaml`에 출처와 라이선스를 반드시 기록합니다.

## 라벨 (최초 1회, 저장소 관리자)
```bash
gh label create "module:crawler"     -c 1f77b4
gh label create "module:law"         -c ff7f0e
gh label create "module:public-data" -c 2ca02c
gh label create "module:framework"   -c 9467bd
gh label create "schema"             -c d62728
gh label create "type:task"          -c cccccc
gh label create "type:bug"           -c b60205
```
