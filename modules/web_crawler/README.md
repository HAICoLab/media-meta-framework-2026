# [1] web_crawler

URL → HTML 수집 → `MetadataRecord` 1건.

- 추출 항목: `<title>`, `meta description/keywords`, `og:*`, `article:published_time`, canonical URL, `lang`
- 원본 HTML은 저장소에 커밋하지 않는다 (`data/raw/`는 gitignore 대상).
- TODO: robots.txt 확인, JS 렌더링 페이지(Playwright), 사이트별 파서 플러그인
