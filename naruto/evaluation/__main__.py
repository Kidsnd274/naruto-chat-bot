"""Model evaluation CLI.

    python -m naruto.evaluation run CASES.json --model qwen3.8-27b@http://gufo:8080/v1 \\
        --model qwen3.8-flash-next@http://gufo:8080/v1 --out ~/naruto-eval/2026-10-01
    python -m naruto.evaluation extract --export result.json --trigger 1234567 \\
        --out ~/naruto-eval/cases/bbq-plan.json

Evaluation cases come from real chats: keep them, and the reports, outside
the repository.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

from naruto.evaluation.cases import CaseError, load_cases
from naruto.evaluation.extract import ExtractError, extract_case
from naruto.evaluation.report import summarize, write_report
from naruto.evaluation.runner import (
    default_client_factory,
    evaluate,
    load_settings_db,
    parse_model,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _warn_if_in_repo(path: Path) -> None:
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return
    print(f"warning: {path} is inside the repository. Evaluation data comes from real "
          "chats; keep it outside the repository.", file=sys.stderr)


def _run(args) -> int:
    try:
        cases = load_cases(Path(args.cases))
    except CaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case.id in wanted]
    base_settings = load_settings_db(args.settings_db) if args.settings_db else {}
    default_endpoint = (args.endpoint or base_settings.get("model.endpoint_url")
                        or os.getenv("OPENAI_BASE_URL") or "http://localhost:8080/v1")
    targets = [parse_model(value, default_endpoint) for value in args.model]
    out_dir = Path(args.out).expanduser()
    _warn_if_in_repo(out_dir)
    api_key = args.api_key or os.getenv("OPENAI_API_KEY") or "eval"
    attempts = asyncio.run(evaluate(
        cases, targets,
        client_factory=default_client_factory(api_key, args.timeout),
        base_settings=base_settings, repeat=args.repeat, stream=not args.no_stream,
        keep_prompts=args.keep_prompts, progress=lambda line: print(line, flush=True),
    ))
    results, report = write_report(attempts, cases, targets, out_dir)
    for target in targets:
        s = summarize(attempts, target.label)
        rate = "n/a" if s["pass_rate"] is None else f"{s['pass_rate']:.0%}"
        print(f"{target.label}: auto-checked pass rate {rate}, {s['manual']} to review by hand, "
              f"{s['errors']} errors")
    print(f"Wrote {report} and {results}")
    return 0


def _extract(args) -> int:
    try:
        case = extract_case(Path(args.export).expanduser(), args.trigger, before=args.before,
                            case_id=args.id, category=args.category)
    except (ExtractError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser()
    _warn_if_in_repo(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(case, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out} ({len(case['messages'])} messages before the trigger). "
          "Fill in 'description' and 'expect'.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m naruto.evaluation", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run cases against models and write a report")
    run.add_argument("cases", help="JSON file with the cases")
    run.add_argument("--model", action="append", required=True,
                     help="model ID, optionally NAME@ENDPOINT; repeat to compare models")
    run.add_argument("--endpoint", help="default endpoint for models without @ENDPOINT")
    run.add_argument("--settings-db", help="bot database whose settings (prompts, sampling) to use")
    run.add_argument("--out", required=True, help="output directory (outside the repository)")
    run.add_argument("--repeat", type=int, default=1, help="attempts per case and model")
    run.add_argument("--only", help="comma-separated case IDs to run")
    run.add_argument("--api-key", help="API key (default: OPENAI_API_KEY)")
    run.add_argument("--timeout", type=float, default=300, help="seconds per request")
    run.add_argument("--no-stream", action="store_true", help="don't stream (no TTFT)")
    run.add_argument("--keep-prompts", action="store_true", help="store prompts in results.json")
    run.set_defaults(handler=_run)

    extract = commands.add_parser("extract", help="make a case skeleton from an export")
    extract.add_argument("--export", required=True, help="Telegram Desktop result.json")
    extract.add_argument("--trigger", type=int, required=True, help="export message ID to answer")
    extract.add_argument("--before", type=int, default=60, help="messages before the trigger")
    extract.add_argument("--id", help="case ID")
    extract.add_argument("--category", default="other")
    extract.add_argument("--out", required=True, help="where to write the case JSON")
    extract.set_defaults(handler=_extract)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
