from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mmf_framework.router import default_router


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mmf", description="메타데이터 추출 통합 CLI")
    p.add_argument("sources", nargs="+", help="URL, 법령 .txt, registry .yaml")
    p.add_argument("-o", "--output", type=Path, help="JSONL 출력 경로 (기본: stdout)")
    args = p.parse_args(argv)

    router = default_router()
    out = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        for src in args.sources:
            for rec in router.extract(src):
                out.write(rec.model_dump_json() + "\n")
    finally:
        if args.output:
            out.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
