# [2] law_structurer

법령 TXT → 법령·조·항·호·목 계층 → `MetadataRecord` 여러 건 (`parent_id`로 연결).

| 단위 | 패턴 예 | `extra.level` |
|---|---|---|
| 법령 | 첫 줄 (법령명) | `law` |
| 장·절 | `제1장 총칙` | 레코드 대신 `extra.chapter`/`extra.section`에 기록 |
| 조 | `제2조(정의)`, `제5조의2(...)` | `article` |
| 항 | `①` ~ `⑳` | `paragraph` |
| 호 | `1.` | `item` |
| 목 | `가.` | `subitem` |

TODO: 부칙 분리, 별표·서식(표) 파싱, 개정 이력(`<개정 2024. 1. 1.>`) 추출, 고시 형식 대응
