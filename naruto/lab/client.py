"""Command-line client for the prompt lab's API (python3 -m naruto.lab).

Standard library only, so it runs from the host without the bot's
virtualenv, or inside the container. Prints the API's JSON (``--text`` for a
short readable summary where there is one).

Settings: NARUTO_LAB_URL (default http://127.0.0.1:8765) and the token in
NARUTO_LAB_TOKEN or a file (--token-file, default
~/.config/naruto-lab/token), so it never appears in the process list.

Exit codes: 0 ok, 1 server error, 2 invalid request, 3 refused (needs the
owner, a permission, or the run is waiting or out of budget), 4 the server
can't be reached.
"""

import argparse
import base64
import json
import mimetypes
import os
from pathlib import Path
import sys
import time
from urllib import error, parse, request

DEFAULT_URL = "http://127.0.0.1:8765"
DEFAULT_TOKEN_FILE = Path("~/.config/naruto-lab/token").expanduser()
DEFAULT_EXPORT_DIR = Path("~/naruto-lab").expanduser()
API = "/api/lab/v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ".lab-export.json"
CONFIG_FILES = (".md", ".json")


class ClientError(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------- http

class Client:
    def __init__(self, url: str, token: str, timeout: float = 360):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body=None, **query):
        query = {k: v for k, v in query.items() if v is not None and v is not False}
        url = f"{self.url}{API}{path}"
        if query:
            url += "?" + parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        status, raw = self._send(method, url, data, headers)
        if status < 400:
            return json.loads(raw or b"null")
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        message = payload.get("message") or f"HTTP {status}"
        details = payload.get("details")
        if details:
            message += f" {json.dumps(details, ensure_ascii=False)}"
        code = 3 if status in (401, 403, 409) else 2 if status < 500 else 1
        raise ClientError(f"{status} {payload.get('error', '')}: {message}".strip(), code)

    def _send(self, method: str, url: str, data: bytes | None,
              headers: dict) -> tuple[int, bytes]:
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return response.status, response.read()
        except error.HTTPError as exc:
            return exc.code, exc.read()
        except (error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ClientError(f"Can't reach the bot at {self.url} ({reason}). Is it running, and "
                              "is NARUTO_LAB_URL right?", 4)


def _token(args) -> str:
    if os.getenv("NARUTO_LAB_TOKEN"):
        return os.environ["NARUTO_LAB_TOKEN"].strip()
    path = Path(args.token_file).expanduser() if args.token_file else DEFAULT_TOKEN_FILE
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ClientError(f"No token: set NARUTO_LAB_TOKEN or put it in {path} "
                          "(the owner creates one on the web admin's Lab page).", 2)


# ------------------------------------------------------------------ files

def _read_json(path: str):
    try:
        text = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text("utf-8")
        return json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientError(f"Can't read JSON from {path}: {exc}", 2)


def _inline_images(scenario: dict, base: Path) -> dict:
    """A scenario file may name local images; the API takes them inline."""
    def fix(message):
        image = message.get("image") if isinstance(message, dict) else None
        if image and not str(image).startswith("data:"):
            path = (base / image).expanduser()
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise ClientError(f"Can't read image {path}: {exc}", 2)
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            message = {**message, "image": f"data:{mime};base64,"
                                           f"{base64.b64encode(data).decode('ascii')}"}
        return message

    scenario = dict(scenario)
    for key in ("messages",):
        scenario[key] = [fix(m) for m in scenario.get(key) or []]
    if "trigger" in scenario:
        scenario["trigger"] = fix(scenario["trigger"])
    if "turns" in scenario:
        scenario["turns"] = [{**fix(t), "messages": [fix(m) for m in t.get("messages") or []]}
                             if isinstance(t, dict) else t for t in scenario["turns"]]
        for turn in scenario["turns"]:
            if isinstance(turn, dict) and not turn["messages"]:
                del turn["messages"]
    return scenario


def _scenarios_in(data) -> tuple[list[dict], dict]:
    """A file holds one scenario, a list, or {"defaults": ..., "scenarios"}."""
    if isinstance(data, list):
        return data, {}
    if isinstance(data, dict) and ("scenarios" in data or "cases" in data):
        return list(data.get("scenarios") or data.get("cases") or []), data.get("defaults") or {}
    return [data], {}


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return False
    return (REPO_ROOT / "naruto" / "lab").is_dir()


def write_export(folder: str, files: dict[str, str], out: Path) -> Path:
    """Write the run's folder, removing files an earlier export wrote that
    are gone now (nothing else is touched)."""
    target = out / folder
    target.mkdir(parents=True, exist_ok=True)
    manifest = target / MANIFEST
    try:
        previous = set(json.loads(manifest.read_text("utf-8")))
    except (OSError, ValueError):
        previous = set()
    for name in previous - set(files):
        path = (target / name).resolve()
        if path.is_relative_to(target.resolve()):
            path.unlink(missing_ok=True)
    for name, text in files.items():
        path = (target / name).resolve()
        if not path.is_relative_to(target.resolve()):
            raise ClientError(f"Refusing to write outside {target}: {name}", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    manifest.write_text(json.dumps(sorted(files), indent=1), encoding="utf-8")
    for directory in sorted((p for p in target.rglob("*") if p.is_dir()), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    return target


def read_config_dir(path: Path) -> dict[str, str]:
    files = {}
    for item in sorted(path.iterdir()):
        if item.is_file() and item.suffix in CONFIG_FILES and item.name != "CHANGES.md":
            files[item.name] = item.read_text(encoding="utf-8")
    if not files:
        raise ClientError(f"No configuration files (*.md, settings.json) in {path}.", 2)
    return files


# ---------------------------------------------------------------- output

def _text_batch(view: dict) -> str:
    lines = [f"batch {view['id']} (run {view['run']}): {view['status']}"
             + (" — still running" if view.get("running") else "")]
    for attempt in view.get("attempts", []):
        lines.append(f"  attempt {attempt['id']}  {attempt['scenario']} v"
                     f"{attempt['scenario_version']}  {attempt['candidate']}  try "
                     f"{attempt['repeat']}: {attempt['outcome'] or attempt['status']}"
                     + (f" ({attempt['reason']})" if attempt.get("reason") else ""))
        for index, answer in enumerate(attempt.get("answers") or [], start=1):
            short = " ".join((answer or "(nothing sent)").split())
            lines.append(f"      turn {index}: {short[:200]}")
    return "\n".join(lines)


def _text_run(view: dict) -> str:
    state = view["state"]
    lines = [f"run {view['id']}: {view['objective'][:120]}",
             f"  state: {state['state']}" + (f" ({view['stop_reason']})"
                                              if view.get("stop_reason") else ""),
             f"  model: {view['model']['name']} at {view['model']['endpoint']}",
             f"  used: {state['used']['attempts']}/{state['budget']['attempts']} attempts, "
             f"{state['used']['model_requests']}/{state['budget']['model_requests']} requests"]
    if state.get("pending_comparisons"):
        lines.append(f"  waiting for the owner on comparison(s) {state['pending_comparisons']}")
    for warning in state.get("warnings") or []:
        lines.append(f"  warning: {warning}")
    for candidate in view.get("candidates", []):
        lines.append(f"  {candidate['label']} (on {candidate['parent']}): "
                     f"{', '.join(candidate['changes'])}")
    return "\n".join(lines)


TEXT = {"batch": _text_batch, "run": _text_run}


def _print(result, args, kind: str | None = None) -> None:
    if args.text and kind in TEXT and isinstance(result, dict):
        print(TEXT[kind](result))
    elif args.text and isinstance(result, dict) and "presentation" in result:
        print(result["presentation"])
    elif args.text and isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False))


# --------------------------------------------------------------- commands

def _wait(client: Client, batch_id: int, timeout: float, quiet: bool = False) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        view = client.call("GET", f"/batches/{batch_id}", wait=max(1, min(60, int(left))))
        if view["status"] not in ("queued", "running") and not view.get("running"):
            return view
        if time.monotonic() >= deadline:
            return view
        if not quiet:
            done = sum(1 for a in view["attempts"] if a["status"] not in ("queued", "running"))
            print(f"batch {batch_id}: {done}/{len(view['attempts'])} attempts finished…",
                  file=sys.stderr)


def run_command(args, client: Client):
    command = args.command
    if command == "capabilities":
        return client.call("GET", "/capabilities"), None
    if command == "config":
        return client.call("GET", "/config/active"), None
    if command == "run":
        if args.action == "start":
            return client.call("POST", "/runs", _read_json(args.file)), "run"
        if args.action == "list":
            return client.call("GET", "/runs"), None
        if args.action == "show":
            return client.call("GET", f"/runs/{args.run}"), "run"
        if args.action == "stop":
            return client.call("POST", f"/runs/{args.run}/stop",
                               {"reason": args.reason, "note": args.note}), "run"
        if args.action == "finish":
            summary = args.summary
            if args.summary_file:
                summary = Path(args.summary_file).expanduser().read_text("utf-8")
            recommendation = None
            if args.recommend:
                recommendation = {"action": args.recommend, "candidate": args.candidate,
                                  "text": args.why}
            return client.call("POST", f"/runs/{args.run}/finish",
                               {"reason": args.reason, "recommendation": recommendation,
                                "summary": summary}), "run"
    if command == "candidate":
        if args.action == "add":
            body = {"name": args.name, "parent": args.parent, "hypothesis": args.hypothesis,
                    "rationale": args.rationale}
            if args.from_dir:
                body["files"] = read_config_dir(Path(args.from_dir).expanduser())
            elif args.file:
                body["changes"] = _read_json(args.file)
            else:
                raise ClientError("Give the changes: --file changes.json or --from-dir DIR.", 2)
            return client.call("POST", f"/runs/{args.run}/candidates", body), None
        if args.action == "show":
            return client.call("GET", f"/runs/{args.run}/candidates/{args.ref}"), None
    if command == "scenario":
        if args.action == "add":
            data = _read_json(args.file)
            base = Path(".") if args.file == "-" else Path(args.file).expanduser().parent
            scenarios, defaults = _scenarios_in(data)
            results = []
            for scenario in scenarios:
                body = _inline_images({**defaults, **scenario}, base)
                results.append(client.call("POST", "/scenarios",
                                           {"scenario": body, "reason": args.reason,
                                            "run": args.run}))
            return results[0] if len(results) == 1 else results, None
        if args.action == "list":
            return client.call("GET", "/scenarios", query=args.query), None
        if args.action == "show":
            return client.call("GET", f"/scenarios/{args.ref}"), None
    if command == "set":
        if args.action == "add":
            return client.call("POST", f"/runs/{args.run}/sets",
                               {"name": args.name, "purpose": args.purpose,
                                "scenarios": args.scenarios}), None
        if args.action == "change":
            remove = [{"slug": slug, "reason": args.reason} for slug in args.remove or []]
            return client.call("POST", f"/runs/{args.run}/sets/{args.name}",
                               {"add": args.add or [], "remove": remove}), None
    if command in ("try", "suite"):
        body = {"candidates": [_candidate(c) for c in (args.candidate or ["baseline"])],
                "repeat": args.repeat, "owner_request": args.owner_request,
                "continue_from": getattr(args, "continue_from", None)}
        if command == "try":
            body["scenarios"] = args.scenario
        else:
            body["set"] = args.set
        view = client.call("POST", f"/runs/{args.run}/attempts", body)
        if args.wait:
            view = _wait(client, view["id"], args.wait, quiet=args.quiet)
        return view, "batch"
    if command == "wait":
        return _wait(client, args.batch, args.timeout, quiet=args.quiet), "batch"
    if command == "batch":
        return client.call("GET", f"/batches/{args.batch}"), "batch"
    if command == "cancel":
        return client.call("POST", f"/batches/{args.batch}/cancel"), "batch"
    if command == "resume":
        return client.call("POST", f"/batches/{args.batch}/resume"), "batch"
    if command == "attempt":
        return client.call("GET", f"/attempts/{args.attempt}", prompts=args.prompts), None
    if command == "export":
        out = Path(args.out).expanduser() if args.out else DEFAULT_EXPORT_DIR
        if _inside_repo(out):
            print(f"warning: {out} is inside the repository. Lab results can contain real "
                  "chat content; keep them outside it.", file=sys.stderr)
        data = client.call("GET", f"/runs/{args.run}/export")
        target = write_export(data["folder"], data["files"], out)
        return f"Wrote {len(data['files'])} files to {target}", None
    raise ClientError(f"Unknown command {command}.", 2)


def _candidate(value: str):
    return None if value in ("baseline", "c0", "0") else value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m naruto.lab", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.getenv("NARUTO_LAB_URL", DEFAULT_URL))
    parser.add_argument("--token-file", help=f"file with the token (default {DEFAULT_TOKEN_FILE})")
    parser.add_argument("--text", action="store_true", help="readable output where available")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("capabilities", help="what the lab supports and every tunable setting")
    commands.add_parser("config", help="the live values of the tunable settings")

    run = commands.add_parser("run", help="start, show, stop or finish a run")
    run_actions = run.add_subparsers(dest="action", required=True)
    start = run_actions.add_parser("start", help="start a run from a JSON spec")
    start.add_argument("--file", required=True, help="the run spec (JSON; - for stdin)")
    run_actions.add_parser("list")
    show = run_actions.add_parser("show")
    show.add_argument("run", type=int)
    stop = run_actions.add_parser("stop", help="stop a run (cancels running attempts)")
    stop.add_argument("run", type=int)
    stop.add_argument("--reason", default="cancelled",
                      choices=["objective_met", "budget_exhausted", "no_improvement", "blocked",
                               "cancelled"])
    stop.add_argument("--note")
    finish = run_actions.add_parser("finish", help="end a run with a recommendation")
    finish.add_argument("run", type=int)
    finish.add_argument("--reason", required=True,
                        choices=["objective_met", "budget_exhausted", "no_improvement",
                                 "blocked", "cancelled"])
    finish.add_argument("--recommend", choices=["activate", "continue", "keep_baseline"])
    finish.add_argument("--candidate", help="the candidate recommended for activation")
    finish.add_argument("--why", help="one paragraph: why this recommendation")
    finish.add_argument("--summary")
    finish.add_argument("--summary-file")

    candidate = commands.add_parser("candidate", help="add or show a candidate configuration")
    candidate_actions = candidate.add_subparsers(dest="action", required=True)
    add = candidate_actions.add_parser("add")
    add.add_argument("run", type=int)
    add.add_argument("--name", required=True)
    add.add_argument("--file", help="JSON object of changes: {\"setting.key\": value}")
    add.add_argument("--from-dir", help="a configuration folder (as lab export writes them)")
    add.add_argument("--parent", help="the candidate it builds on (default: the baseline)")
    add.add_argument("--hypothesis", default="")
    add.add_argument("--rationale", default="")
    candidate_show = candidate_actions.add_parser("show")
    candidate_show.add_argument("run", type=int)
    candidate_show.add_argument("ref", help="c1, a candidate id, or baseline")

    scenario = commands.add_parser("scenario", help="add, list or show scenarios")
    scenario_actions = scenario.add_subparsers(dest="action", required=True)
    scenario_add = scenario_actions.add_parser("add")
    scenario_add.add_argument("--file", required=True, help="scenario JSON (one, a list, or "
                                                             "{\"scenarios\": [...]}); - for stdin")
    scenario_add.add_argument("--reason", help="why an existing scenario changes")
    scenario_add.add_argument("--run", type=int, help="the run the change belongs to")
    scenario_list = scenario_actions.add_parser("list")
    scenario_list.add_argument("--query")
    scenario_show = scenario_actions.add_parser("show")
    scenario_show.add_argument("ref", help="slug or id")

    sets = commands.add_parser("set", help="sets of scenarios: tuning, validation, regression")
    set_actions = sets.add_subparsers(dest="action", required=True)
    set_add = set_actions.add_parser("add")
    set_add.add_argument("run", type=int)
    set_add.add_argument("--name", required=True)
    set_add.add_argument("--purpose", required=True,
                         choices=["tuning", "validation", "regression"])
    set_add.add_argument("scenarios", nargs="*", help="scenario slugs")
    set_change = set_actions.add_parser("change")
    set_change.add_argument("run", type=int)
    set_change.add_argument("name")
    set_change.add_argument("--add", action="append")
    set_change.add_argument("--remove", action="append")
    set_change.add_argument("--reason", help="why scenarios are removed (required to remove)")

    for name, helptext in (("try", "run scenarios under one or more configurations"),
                           ("suite", "run a set under one or more configurations")):
        sub = commands.add_parser(name, help=helptext)
        sub.add_argument("run", type=int)
        if name == "try":
            sub.add_argument("--scenario", action="append", required=True,
                             help="slug or id (repeat for several)")
            sub.add_argument("--continue-from", type=int,
                             help="continue the conversation of this attempt")
        else:
            sub.add_argument("--set", required=True)
        sub.add_argument("--candidate", action="append",
                         help="c1, an id, or baseline (repeat to compare; default baseline)")
        sub.add_argument("--repeat", type=int, default=1)
        sub.add_argument("--owner-request",
                         help="what the owner asked for, when the run waits for their choice")
        sub.add_argument("--wait", type=float, default=0, help="wait up to this many seconds")
        sub.add_argument("--quiet", action="store_true", help="no progress lines")

    wait = commands.add_parser("wait", help="wait for a batch to finish")
    wait.add_argument("batch", type=int)
    wait.add_argument("--timeout", type=float, default=3600)
    wait.add_argument("--quiet", action="store_true")
    for name in ("batch", "cancel", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("batch", type=int)
    attempt = commands.add_parser("attempt", help="everything about one attempt")
    attempt_actions = attempt.add_subparsers(dest="action", required=True)
    attempt_show = attempt_actions.add_parser("show")
    attempt_show.add_argument("attempt", type=int)
    attempt_show.add_argument("--prompts", action="store_true",
                              help="include the exact prompts and every step")
    export = commands.add_parser("export", help="write a run as a folder of readable files")
    export.add_argument("run", type=int)
    export.add_argument("--out", help=f"where (default {DEFAULT_EXPORT_DIR})")
    return parser


def main(argv=None, client: Client | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = client or Client(args.url, _token(args))
        result, kind = run_command(args, client)
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    _print(result, args, kind)
    return 0
