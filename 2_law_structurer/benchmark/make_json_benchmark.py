"""심의사례 JSON에서 "방송 전 사전 제작안"을 생성해 벤치마크 JSON으로 저장한다.

실행: python make_json_benchmark.py ../case_db/output/cases output/benchmark.json \
          --base-url http://localhost:8000/v1 --model <모델 이름>
사례 읽기 → 방송 기록 구성(의결번호·관련 조항·심의결과 제외) → 모델 호출 → 결과 분류 → 저장.
모델은 OpenAI 호환 서버(/chat/completions)를 쓴다. API 키는 환경변수로 준다.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
MARKER = re.compile(r'<!--\s*(system|example input|example output)\s*-->')
EXCLUDED = '제외:'


def read_cases(path):
    """사례 JSON 파일 하나 또는 폴더를 읽어 사례 리스트로 만든다."""
    path = Path(path)
    files = sorted(path.glob('*.json')) if path.is_dir() else [path]
    if not files:
        raise ValueError(f'사례 JSON이 없습니다: {path}')
    cases = []
    for file in files:
        cases.extend(json.loads(file.read_text(encoding='utf-8')))
    return cases


def case_text(case):
    """모델에 주는 방송 기록. 의결번호·관련 조항·심의결과는 넣지 않는다."""
    meta = case['case_metadata']
    times = []
    for slot in meta.get('broadcast_time') or []:
        span = f"{slot.get('start_time') or ''}~{slot.get('end_time') or ''}".strip('~')
        times.append(f"{slot.get('date') or ''} {span}".strip())
    lines = [f"방송사: {meta.get('broadcaster') or '-'}"]
    if meta.get('channel'):
        lines.append(f"채널: {meta['channel']}")
    lines.append(f"프로그램: {meta.get('program_name') or '-'}")
    lines.append(f"방송일시: {', '.join(times) or '-'}")
    return '\n'.join(lines) + '\n\n[방송 기록]\n' + case['case_decision']['content']


def read_prompt(path):
    """prompt.md를 대화 목록(시스템 지시 + 예시 입력·출력 쌍)으로 바꾼다."""
    parts = MARKER.split(Path(path).read_text(encoding='utf-8'))
    roles = {'system': 'system', 'example input': 'user', 'example output': 'assistant'}
    messages = [{'role': roles[kind], 'content': body.strip()} for kind, body in zip(parts[1::2], parts[2::2])]
    order = [m['role'] for m in messages]
    if order[:1] != ['system'] or order[1:] != ['user', 'assistant'] * ((len(order) - 1) // 2):
        raise ValueError(f'prompt.md 표식 순서가 잘못됐습니다: {order}')
    return messages


def call_model(base_url, api_key, payload, timeout):
    request = urllib.request.Request(
        base_url.rstrip('/') + '/chat/completions', data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def generate(case, messages, args, api_key):
    """사례 하나의 제작안을 만든다. 실패도 레코드로 남긴다."""
    payload = {'model': args.model, 'messages': messages + [{'role': 'user', 'content': case_text(case)}],
               'max_tokens': args.max_tokens, 'temperature': args.temperature}
    if args.no_thinking:
        payload['chat_template_kwargs'] = {'enable_thinking': False}
    started, text, reason, usage = time.time(), '', None, {}
    try:
        body = call_model(args.base_url, api_key, payload, args.timeout)
        choice = body['choices'][0]
        text, reason, usage = (choice['message'].get('content') or '').strip(), choice.get('finish_reason'), body.get('usage', {})
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
        reason = f'error: {exc}'
    if text.startswith(EXCLUDED):
        status = 'excluded'
    else:
        status = 'ok' if text and reason == 'stop' else 'failed'
    record = {
        'case_id': case['case_id'],
        'status': status,
        'scenario': text if status == 'ok' else None,
        'provisions': case['case_decision']['provisions'],
        'result': case['case_decision']['result'],
        'source': {'file': case['source']['file'], 'category': case['source'].get('category')},
    }
    if status == 'excluded':
        record['excluded_reason'] = text
    log = {'case_id': case['case_id'], 'status': status, 'finish_reason': reason,
           'input_tokens': usage.get('prompt_tokens'), 'output_tokens': usage.get('completion_tokens'),
           'seconds': round(time.time() - started, 1)}
    return record, log


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('input', help='사례 JSON 파일 또는 폴더 (case_db/output/cases)')
    parser.add_argument('output', help='저장할 벤치마크 JSON 파일')
    parser.add_argument('--base-url', required=True, help='OpenAI 호환 서버 주소 (…/v1)')
    parser.add_argument('--model', required=True)
    parser.add_argument('--api-key-env', default='OPENAI_API_KEY', help='API 키가 든 환경변수 이름')
    parser.add_argument('--prompt', default=str(ROOT / 'prompt.md'))
    parser.add_argument('--case-id', action='append', default=[], help='이 의결번호만 생성 (여러 번 지정 가능)')
    parser.add_argument('--limit', type=int, help='앞에서 N건만')
    parser.add_argument('--max-tokens', type=int, default=16000)
    parser.add_argument('--temperature', type=float, default=1.0)
    parser.add_argument('--timeout', type=int, default=900, help='호출 하나의 제한 시간(초)')
    parser.add_argument('--no-thinking', action='store_true', help='추론을 끌 수 있는 서버(vLLM의 Qwen 등)에서 추론을 끈다')
    parser.add_argument('--workers', type=int, default=4, help='동시에 보내는 호출 수')
    parser.add_argument('--overwrite', action='store_true', help='이미 생성된 사례도 다시 생성한다')
    args = parser.parse_args()

    cases = [c for c in read_cases(args.input) if c['case_decision'].get('content')]
    if args.case_id:
        known = {c['case_id'] for c in cases}
        missing = [i for i in args.case_id if i not in known]
        if missing:
            raise SystemExit(f'입력에 없는 의결번호: {missing}')
        cases = [c for c in cases if c['case_id'] in args.case_id]
    cases = cases[:args.limit]
    messages = read_prompt(args.prompt)
    prompt_sha = hashlib.sha256(Path(args.prompt).read_bytes()).hexdigest()
    api_key = os.environ.get(args.api_key_env, 'none')

    output = Path(args.output)
    records = {}
    if output.exists():                                         # 이미 생성된 사례는 유지한다
        records = {r['case_id']: r for r in json.loads(output.read_text(encoding='utf-8'))['cases']}
    todo = [c for c in cases if args.overwrite or records.get(c['case_id'], {}).get('status') not in ('ok', 'excluded')]
    meta = {'model': args.model, 'prompt': Path(args.prompt).name, 'prompt_sha256': prompt_sha,
            'options': {'max_tokens': args.max_tokens, 'temperature': args.temperature, 'thinking': not args.no_thinking}}
    saving = threading.Lock()

    def save():
        meta['created'] = datetime.now().astimezone().isoformat(timespec='seconds')
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps({'meta': meta, 'cases': list(records.values())}, ensure_ascii=False, indent=2) + '\n',
                             encoding='utf-8')
        temporary.replace(output)

    def work(case):
        record, log = generate(case, messages, args, api_key)
        with saving:                                            # 사례가 끝날 때마다 저장한다
            records[record['case_id']] = record
            save()
            print(json.dumps(log, ensure_ascii=False), flush=True)
        return record['status']

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        statuses = list(pool.map(work, todo))
    counts = {}
    for record in records.values():
        counts[record['status']] = counts.get(record['status'], 0) + 1
    print(json.dumps({'output': str(output), 'generated': len(todo), 'kept': len(cases) - len(todo), 'counts': counts},
                     ensure_ascii=False))
    sys.exit(1 if 'failed' in statuses else 0)


if __name__ == '__main__':
    main()
