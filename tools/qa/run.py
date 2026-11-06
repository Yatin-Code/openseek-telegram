"""One QA entry point for the epic campaign (P1, issue #42).

Usage (from the repository root, with the project venv):

    .venv/bin/python tools/qa/run.py list                     # campaign tickets + state from the tracker
    .venv/bin/python tools/qa/run.py run contract-host        # live target (registry + operator's qa.env)
    .venv/bin/python tools/qa/run.py run contract-local       # local target (live-server harness, ephemeral)
    .venv/bin/python tools/qa/run.py run secret-leak-drill --expect-leak
                                                              # negative drill: redact + fail IS the pass

A run prints a per-step PASS/FAIL table, an evidence block ready to paste into
the ticket, and exits 0 when every step passed, 1 on a failed step, and 2 when
the secret guard refused the output (a known secret value appeared in it) or on
a usage error. CI never reads the operator's secret stores; `list` is the only
command that talks to GitHub, and it runs locally by hand.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent
REPO = _TOOLS.parents[1]
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))

from client import Steps                 # noqa: E402
from recipes import RECIPES              # noqa: E402
from secrecy import collect_known_secrets  # noqa: E402

EPIC_TRACKER = "Yatin-Code/openseek-telegram"
CAMPAIGN_TICKETS = 42


def cmd_list(_args: argparse.Namespace) -> int:
    """The campaign tickets straight from the tracker: children of the two epics
    (#41, #40) plus the harness ticket, with state and open-blocker counts."""
    def api(path: str):
        proc = subprocess.run(["gh", "api", path, "--paginate"], capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"`gh api` failed: {proc.stderr.strip()[:200]}")
        return proc.stdout

    def children(epic: int) -> list[dict]:
        rows = json.loads(api(f"repos/{EPIC_TRACKER}/issues/{epic}/sub_issues"))
        return [{"number": r["number"], "title": r["title"], "state": r["state"],
                 "blockers": r.get("issue_dependencies_summary", {}).get("blocked_by", 0)} for r in rows]

    harness = json.loads(api(f"repos/{EPIC_TRACKER}/issues/{CAMPAIGN_TICKETS}"))
    todo = [("Epic G - live reference deployment (#40)", children(40)),
            ("Epic H - acceptance campaign (#41)", children(41)),
            ("Runner (#42)", [{"number": 42, "state": harness["state"],
                               "title": harness["title"], "blockers": 0}])]
    print("%-5s %-7s %-9s %s" % ("id", "state", "blockers", "title"))
    for label, rows in todo:
        print(f"\n{label}")
        for row in sorted(rows, key=lambda r: r["number"]):
            print("%-5s %-7s %-9s %s" % (row["number"], row["state"], row["blockers"], row["title"]))
    return 0


def run_recipe(args: argparse.Namespace) -> int:
    spec = RECIPES.get(args.recipe)
    if spec is None:
        known = "\n  ".join(f"{name} (ticket {v['ticket']}, targets {v['targets']})"
                            for name, v in sorted(RECIPES.items()))
        raise SystemExit(f"unknown recipe {args.recipe!r}. Known recipes:\n  {known}")
    target = args.target or spec["targets"][0]
    if target not in spec["targets"]:
        raise SystemExit(f"recipe {args.recipe} runs on targets {spec['targets']}, not {target!r}")

    steps = Steps()
    kwargs = {"expect_leak": bool(args.expect_leak)}
    import inspect                                   # runner kwargs differ per recipe body
    try:
        signature = inspect.signature(spec["runner"])
        kwargs = {k: kwargs[k] for k in kwargs if k in signature.parameters}
        passed = spec["runner"](steps, **kwargs)
    except TypeError:                                # a defensive fallback: run without extras
        kwargs = {}
        passed = spec["runner"](steps, **kwargs)

    secrets = collect_known_secrets()

    table = [f"QA run: {args.recipe} - {spec['title']} (ticket {spec['ticket']}, target {target})", ""]
    for name, ok, _evidence in steps.rows:
        table.append("%-7s %s" % ("PASS" if ok else "FAIL", name))
    n_pass = sum(1 for _n, ok, _e in steps.rows if ok)
    table.append("")
    table.append(f"{n_pass}/{len(steps.rows)} steps passed."
                 if len(steps.rows) else "no steps recorded")
    blocked = not passed
    if blocked:
        table.append("A failed step blocks sign-off until it is diagnosed and re-run green.")

    evidence = "### Evidence\n```\n" + "\n".join(
        line for _n, _ok, lines in steps.rows for line in lines) + "\n```"

    text, leaked = [], False
    for chunk in ("\n".join(table), evidence):
        clean, hit = scrub_output(chunk, secrets)
        text.append(clean)
        leaked = leaked or hit
    print(text[0])
    print()
    print(text[1])
    expect_leak = bool(args.expect_leak)
    if leaked and expect_leak:
        # The drill planted a known secret and the guard caught it: that IS the pass.
        print("(pass) the guard caught the planted secret value and redacted it - "
              "the runner refuses to publish secrets, as required.")
        return 0
    if expect_leak:
        # --expect-leak was given but the guard did NOT fire: the guard is broken.
        print("(fail) the drill planted a known secret value and the guard did NOT catch it. "
              "This is a defect in the runner's secret guard, not a passing drill.")
        return 1
    if leaked:
        print("(fail) a known secret value appeared in the run output and has been redacted. "
              "Exit 2: the evidence above MUST NOT be pasted into a ticket without a re-run.")
        return 2
    return 0 if passed else 1


def scrub_output(text: str, secrets: list[str]) -> tuple[str, bool]:
    from secrecy import scrub
    return scrub(text, secrets)


def main() -> int:
    parser = argparse.ArgumentParser(prog="tools/qa/run.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list the campaign tickets and their state from the tracker")
    run_p = sub.add_parser("run", help="run a named recipe against its target")
    run_p.add_argument("recipe", help=f"one of: {', '.join(sorted(RECIPES))}")
    run_p.add_argument("--target", choices=("local", "live", "drill"), default=None,
                       help="override the recipe's default target")
    run_p.add_argument("--expect-leak", action="store_true",
                       help="secret-leak-drill: the guard catching a leak is the expected outcome")
    args = parser.parse_args()
    return cmd_list(args) if args.cmd == "list" else run_recipe(args)


if __name__ == "__main__":
    raise SystemExit(main())
