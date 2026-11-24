"""문서별 표: 조항 ID 앞머리와 원문 보정.

조항 ID는 본문의 조·항·호·목마다 붙는다(부칙·별표에는 붙지 않는다).
  BR_REVIEW_ART48          방송심의에 관한 규정 제48조
  BR_REVIEW_ART48_P1_I2    제48조 제1항 제2호
  BR_REVIEW_ART38_2_P1     제38조의2 제1항
  BR_REVIEW_ART2_I1_A      제2조 제1호 가목
새 문서를 추가하면 DOCUMENT_IDS에 한 줄을 더한다. 표에 없는 문서는 변환하지 않는다.
"""
import re

# 문서명 → ID 앞머리. 형식: <주제>_<종류>. 종류는 REVIEW(심의규정)·RULE(규칙)·NOTICE(고시)·STD(기준)·
# ACT(법률)·DECREE(시행령)·ENFORCE(시행규칙).
DOCUMENT_IDS = {
    '방송심의에 관한 규정': 'BR_REVIEW',
    '방송광고심의에 관한 규정': 'AD_REVIEW',
    '상품소개 및 판매방송 심의에 관한 규정': 'SALES_REVIEW',
    '선거방송심의에 관한 특별규정': 'ELECTION_REVIEW',
    '정보통신에 관한 심의규정': 'ICT_REVIEW',
    '협찬고지 등에 관한 규칙': 'SPONSOR_RULE',
    '방송평가에 관한 규칙': 'EVAL_RULE',
    '방송프로그램의 등급분류 및 표시 등에 관한 규칙': 'RATING_RULE',
    '방송프로그램 등의 편성에 관한 고시': 'SCHEDULE_NOTICE',
    '가상광고 세부기준 등에 관한 고시': 'VIRTUAL_AD_NOTICE',
    '공익채널 선정 및 장애인복지채널 인정에 관한 고시': 'PUBLIC_CHANNEL_NOTICE',
    '방송광고 결합판매 지원고시': 'AD_BUNDLE_NOTICE',
    '연속편성 세부기준 등에 관한 고시': 'CONSECUTIVE_NOTICE',
    '외주제작사의 간접광고 판매 위탁에 관한 고시': 'PPL_OUTSOURCE_NOTICE',
    '장애인방송 편성 및 제공 등 장애인 방송접근권 보장에 관한 고시': 'ACCESS_NOTICE',
    '방송광고판매대행 등에 관한 법률에 따른 과징금 부과 세부기준': 'AD_AGENCY_FINE_STD',
    '보편적 시청권 관련 금지행위 세부기준': 'UNIVERSAL_ACCESS_STD',
    '불법촬영물등 유통방지를 위한 기술적·관리적 조치 기준': 'ILLEGAL_FOOTAGE_STD',
    '시청점유율 산정 등에 관한 기준': 'AUDIENCE_SHARE_STD',
    '재난방송 및 민방위경보방송의 실시에 관한 기준': 'DISASTER_STD',
    '영화 및 비디오물의 진흥에 관한 법률': 'FILM_ACT',
    '영화 및 비디오물의 진흥에 관한 법률 시행령': 'FILM_DECREE',
    '영화 및 비디오물의 진흥에 관한 법률 시행규칙': 'FILM_ENFORCE',
}

# 원문 보정. 원문(국가법령정보센터 TXT)에 번호가 빠진 줄을 사용자 확인을 거쳐 고친다.
# (문서명, 원문 줄) → (고친 줄, 근거). 원문 대조 검증도 고친 줄을 기준으로 한다.
CORRECTIONS = {
    ('방송심의에 관한 규정', '방송은 바른말을 사용하여 국민의 바른 언어생활에 이바지하여야 한다.'):
        ('⑯ 방송은 바른말을 사용하여 국민의 바른 언어생활에 이바지하여야 한다.',
         '제7조 마지막 문장의 항 번호 ⑯ 누락 (2026-11-06 사용자 확인)'),
    ('방송심의에 관한 규정', '제38조의2(자살묘사) ⓛ 방송은 자살장면을 직접적으로 묘사하거나 자살의 수단·방법·장소를 구체적으로 묘사하여서는 아니 되며, 사건 현장을 자극적으로 묘사하지 않도록 주의하여야 한다.'):
        ('제38조의2(자살묘사) ① 방송은 자살장면을 직접적으로 묘사하거나 자살의 수단·방법·장소를 구체적으로 묘사하여서는 아니 되며, 사건 현장을 자극적으로 묘사하지 않도록 주의하여야 한다.',
         '제38조의2 제1항 번호가 ①이 아닌 ⓛ(동그라미 l)로 적힌 오기'),
}

SUBITEM_ORDER = '가나다라마바사아자차카타파하거너더러머버서어저처커터퍼허'   # common.SUBITEM과 같은 범위
_ARTICLE = re.compile(r'제(\d+)조(?:의(\d+))?')
_PARAGRAPH = re.compile(r'제(\d+)항')
_ITEM = re.compile(r'제(\d+)(?:의(\d+))?호')   # 이 DB의 호 라벨 표기: "제4호", "제4의2호"
_SUBITEM = re.compile(rf'([{SUBITEM_ORDER}])목')


def prefix(document_name):
    if document_name not in DOCUMENT_IDS:
        raise ValueError(f'조항 ID 앞머리가 없는 문서입니다. document_rules/documents.py에 추가하십시오: {document_name}')
    return DOCUMENT_IDS[document_name]


def correct(document_name, rows):
    """본문 줄에 원문 보정을 적용한다. (고친 줄 목록, 보정 기록)을 돌려준다.

    이 문서의 보정 항목은 정확히 한 줄에 한 번씩 적용돼야 한다. 원문이 바뀌어 맞는 줄이 없거나 같은 줄이
    여러 번 나오면 변환을 멈춘다(엉뚱한 줄을 고치거나 보정이 조용히 빠지는 것을 막는다).
    """
    out, notes, used = [], [], {}
    for row in rows:
        key = (document_name, row['text'].strip())
        fix = CORRECTIONS.get(key)
        if fix:
            used[key] = used.get(key, 0) + 1
            row = {**row, 'text': fix[0]}
            notes.append({'source_line': row['source_line'], 'reason': f'원문 보정: {fix[1]}'})
        out.append(row)
    for key in CORRECTIONS:
        if key[0] == document_name and used.get(key) != 1:
            raise ValueError(f'원문 보정 대상 줄이 {used.get(key, 0)}번 나옵니다(1번이어야 함): {key[1][:40]}')
    return out, notes


def part(node):
    """노드 라벨 → ID 조각 (ART48, P1, I4_2, A)."""
    label, kind = node['label'], node['type']
    patterns = {'article': (_ARTICLE, 'ART'), 'paragraph': (_PARAGRAPH, 'P'), 'item': (_ITEM, 'I')}
    if kind == 'subitem':
        match = _SUBITEM.match(label)
        if not match:
            raise ValueError(f'목 라벨을 읽을 수 없습니다: {label}')
        index = SUBITEM_ORDER.index(match[1])
        return chr(ord('A') + index) if index < 26 else 'A' + chr(ord('A') + index - 26)   # 거목 다음은 AA…
    pattern, head = patterns[kind]
    match = pattern.match(label)
    if not match:
        raise ValueError(f'{kind} 라벨을 읽을 수 없습니다: {label}')
    return head + match[1] + (f'_{match[2]}' if match.lastindex and match.lastindex >= 2 and match[2] else '')


def assign_ids(node, parent_id):
    """노드와 자손에 ID를 붙인다. ID는 노드의 첫 필드로 둔다."""
    node_id = f'{parent_id}_{part(node)}'
    children = [assign_ids(child, node_id) for child in node['children']]
    return {'id': node_id, **{k: (children if k == 'children' else v) for k, v in node.items() if k != 'id'}}
