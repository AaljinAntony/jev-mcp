"""Routing quality + token harness for the Jev selection tools.

Runs the labelled fixture set in `tests/fixtures/routing_tasks.json` against the
current workspace and reports the metrics that Phase 3 moves:

    top-1 accuracy        files[0] == expected_file
    top-3 recall          expected_file in ranked[:3]
    false-positive rate   matched is True while expected_file is null
    input tokens / call   mean usage.input_tokens
    requests / call       Jev round trips per tool call (must stay 1)

and the same four metrics for `select_mcp_tools` over
`tests/fixtures/mcp_selection_tasks.json`, whose candidates are supplied by the
fixture rather than discovered, so those numbers do not depend on the workspace:

    mcp_top1_accuracy     primary.server == expected_mcp
    mcp_false_positive    matched is True while expected_mcp is null
    mcp_ambiguous_rate    how often a tight choice was reported as `ambiguous`
    mcp_requests_per_call Jev round trips per call (must stay 1)

Usage:
    & .\\.venv\\Scripts\\python.exe scripts\\eval_routing.py --mode mock
    & .\\.venv\\Scripts\\python.exe scripts\\eval_routing.py --mode live
    & .\\.venv\\Scripts\\python.exe scripts\\eval_routing.py --mode live --json

`mock` never touches the network. `live` needs `TYPESAFE_API_KEY` and spends one
Jev round trip per case per tool.
"""

import argparse
import json
import os
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

FIXTURES = REPO / "tests" / "fixtures" / "routing_tasks.json"
MCP_FIXTURES = REPO / "tests" / "fixtures" / "mcp_selection_tasks.json"

os.environ.setdefault("JEV_MCP_MOCK", "1")

import config  # noqa: E402
import jev_engine  # noqa: E402
import jev_logging  # noqa: E402
import logging  # noqa: E402


class _Counter:
    """Counts Jev round trips so a regression to two requests is visible."""

    def __init__(self):
        self.calls = 0
        self.input_tokens = []
        self.output_tokens = []
        self._original = jev_engine.execute_system_one

    def __enter__(self):
        def wrapped(*args, **kwargs):
            self.calls += 1
            res = self._original(*args, **kwargs)
            usage = getattr(res, "usage", None)
            if usage is not None:
                self.input_tokens.append(getattr(usage, "input_tokens", None))
                self.output_tokens.append(getattr(usage, "output_tokens", None))
            return res

        jev_engine.execute_system_one = wrapped
        return self

    def __exit__(self, *exc):
        jev_engine.execute_system_one = self._original
        return False


def load_cases():
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def load_mcp_cases():
    return json.loads(MCP_FIXTURES.read_text(encoding="utf-8"))


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def evaluate_mcp(mode, verbose=False):
    """The same metrics for `select_mcp_tools`, over its own labelled set.

    The roster is a fixture, so this is independent of the workspace: the
    numbers say how the tool routes MCPs, not what happens to be on disk here.
    The fixture's own `jev-engine` entry is the anti-loop check — a run where the
    judge ever picks itself is a broken run, not a low score.
    """
    jev_logging.get_logger().setLevel(logging.WARNING)

    cases = load_mcp_cases()
    positive = [c for c in cases if c.get("expected_mcp")]
    negative = [c for c in cases if not c.get("expected_mcp")]

    rows = []
    tokens_in = []
    requests = 0

    for case in cases:
        with _Counter() as counter:
            res = jev_engine.select_mcp_tools(case["task"], case["roster"], root_dir=str(REPO))
        requests += counter.calls
        tokens_in.extend(counter.input_tokens)

        primary = (res.get("primary") or {}).get("server")
        expected = case.get("expected_mcp")
        # `acceptable_mcps` widens the label for a task where two servers are
        # genuinely interchangeable. Naming the right one out of two is not a
        # miss, and a fixture that pretends otherwise just makes the metric lie.
        acceptable = case.get("acceptable_mcps") or ([expected] if expected else [])
        row = {
            "id": f"mcp:{case['id']}",
            "mode": mode,
            "expected_mcp": expected,
            "acceptable_mcps": acceptable,
            "top1_mcp": primary,
            "mcp_hit1": bool(expected) and primary == expected,
            "mcp_acceptable": bool(acceptable) and primary in acceptable,
            "mcp_false_positive": (not expected) and bool(res.get("matched")),
            "expected_ambiguous": bool(case.get("expected_mcp_ambiguous")),
            "exists": res.get("exists"),
            "ambiguous": res.get("exists") == "ambiguous",
            "judge_picked": primary == jev_engine.SERVER_NAME,
            "tools": [entry["tool"] for entry in res.get("tools", [])],
            "reason_codes": res.get("reason_codes", []),
            "action": res.get("action"),
            "input_tokens": res.get("usage", {}).get("input_tokens"),
        }
        rows.append(row)
        if verbose:
            print(json.dumps(row))

    summary = {
        "mcp_cases": len(cases),
        "mcp_positive_cases": len(positive),
        "mcp_negative_cases": len(negative),
        "mcp_top1_accuracy": _rate(sum(1 for r in rows if r["mcp_hit1"]), len(positive)),
        "mcp_acceptable_rate": _rate(sum(1 for r in rows if r["mcp_acceptable"]), len(positive)),
        "mcp_false_positive_rate": _rate(
            sum(1 for r in rows if r["mcp_false_positive"]), len(negative)
        ),
        "mcp_ambiguous_rate": _rate(sum(1 for r in rows if r["ambiguous"]), len(rows)),
        "mcp_ambiguous_recall": _rate(
            sum(1 for r in rows if r["ambiguous"] and r["expected_ambiguous"]),
            sum(1 for r in rows if r["expected_ambiguous"]),
        ),
        "mcp_judge_picks": sum(1 for r in rows if r["judge_picked"]),
        "mcp_matched_without_tools": sum(
            1 for r in rows if r["exists"] == "answered" and not r["tools"]
        ),
        "mcp_input_tokens_per_call": round(statistics.mean(tokens_in), 1) if tokens_in else None,
        "mcp_input_tokens_median": round(statistics.median(tokens_in), 1) if tokens_in else None,
        "mcp_requests_per_call": _rate(requests, len(cases)),
    }
    return summary, rows


def evaluate(root_dir, mode, verbose=False):
    jev_logging.get_logger().setLevel(logging.WARNING)

    cases = load_cases()
    file_cases = [c for c in cases if c.get("expected_file")]
    null_cases = [c for c in cases if not c.get("expected_file")]
    skill_cases = [c for c in cases if c.get("expected_skill")]

    rows = []
    tokens_in = []
    requests = 0
    calls = 0

    for case in cases:
        with _Counter() as files_counter:
            files_res = jev_engine.select_target_files(case["task"], root_dir=root_dir)
        with _Counter() as skills_counter:
            skills_res = jev_engine.find_agent_resources(case["task"], root_dir=root_dir)

        requests += files_counter.calls + skills_counter.calls
        calls += 2

        ranked = [r["file"] for r in files_res.get("ranked", [])]
        top1 = files_res.get("files")[0] if files_res.get("files") else None
        skill_ranked = [r["file"] for r in skills_res.get("ranked", [])]
        skill_top1 = skill_ranked[0] if skill_ranked else None

        row = {
            "id": case["id"],
            "mode": mode,
            "expected_file": case.get("expected_file"),
            "top1_file": top1,
            "hit1": bool(case.get("expected_file")) and top1 == case["expected_file"],
            "hit3": bool(case.get("expected_file")) and case["expected_file"] in ranked[:3],
            "false_positive": (not case.get("expected_file")) and bool(files_res.get("matched")),
            "expected_skill": case.get("expected_skill"),
            "top1_skill": skill_top1,
            "skill_hit1": bool(case.get("expected_skill")) and skill_top1 == case["expected_skill"],
            "exists": files_res.get("exists"),
            "ranked_top": ranked[0] if ranked else None,
            "action": files_res.get("action"),
            "input_tokens": files_res.get("usage", {}).get("input_tokens"),
            "skill_input_tokens": skills_res.get("usage", {}).get("input_tokens"),
        }
        rows.append(row)
        if row["input_tokens"] is not None:
            tokens_in.append(row["input_tokens"])
        if row["skill_input_tokens"] is not None:
            tokens_in.append(row["skill_input_tokens"])
        if verbose:
            print(json.dumps(row))

    summary = {
        "mode": mode,
        "cases": len(cases),
        "file_cases": len(file_cases),
        "top1_accuracy": _rate(sum(1 for r in rows if r["hit1"]), len(file_cases)),
        "top3_recall": _rate(sum(1 for r in rows if r["hit3"]), len(file_cases)),
        "false_positive_rate": _rate(sum(1 for r in rows if r["false_positive"]), len(null_cases)),
        "partial_rate": _rate(sum(1 for r in rows if r["exists"] == "partial"), len(cases)),
        # A file was the top choice but the verdict refuses to call it answered:
        # the presence Noul disagreed with the Choice. This is the fail-closed
        # behaviour a `none` option inside a Choice cannot produce.
        "downgrade_rate": _rate(
            sum(1 for r in rows if r["exists"] == "partial" and r["ranked_top"]), len(cases)
        ),
        "skill_top1_accuracy": _rate(sum(1 for r in rows if r["skill_hit1"]), len(skill_cases)),
        "input_tokens_per_call": round(statistics.mean(tokens_in), 1) if tokens_in else None,
        "input_tokens_median": round(statistics.median(tokens_in), 1) if tokens_in else None,
        "requests_per_call": _rate(requests, calls),
    }
    return summary, rows


def format_table(summary, rows, mcp_summary=None, mcp_rows=None):
    lines = []
    if "cases" in summary:
        lines += [
            f"mode={summary['mode']}  cases={summary['cases']}  file_cases={summary['file_cases']}",
            "",
            f"{'metric':<24}{'value':>10}",
            f"{'top1_accuracy':<24}{summary['top1_accuracy']:>10}",
            f"{'top3_recall':<24}{summary['top3_recall']:>10}",
            f"{'false_positive_rate':<24}{summary['false_positive_rate']:>10}",
            f"{'partial_rate':<24}{summary['partial_rate']:>10}",
            f"{'downgrade_rate':<24}{summary['downgrade_rate']:>10}",
            f"{'skill_top1_accuracy':<24}{str(summary['skill_top1_accuracy']):>10}",
            f"{'input_tokens_per_call':<24}{str(summary['input_tokens_per_call']):>10}",
            f"{'requests_per_call':<24}{str(summary['requests_per_call']):>10}",
            "",
            f"{'id':<26}{'hit1':>6}{'hit3':>6}{'fp':>4}{'exists':>10}{'tokens':>9}  top1",
        ]
        for r in rows:
            lines.append(
                f"{r['id']:<26}{'y' if r['hit1'] else '-':>6}"
                f"{'y' if r['hit3'] else '-':>6}"
                f"{'y' if r['false_positive'] else '-':>4}"
                f"{str(r['exists']):>10}{str(r['input_tokens']):>9}  {r['top1_file']}"
            )
    if mcp_summary:
        lines += [
            "",
            f"--- select_mcp_tools: {mcp_summary['mcp_cases']} cases, "
            f"{mcp_summary['mcp_positive_cases']} with a server, "
            f"{mcp_summary['mcp_negative_cases']} needing none ---",
            f"{'metric':<24}{'value':>10}",
            f"{'mcp_top1_accuracy':<24}{str(mcp_summary['mcp_top1_accuracy']):>10}",
            f"{'mcp_acceptable_rate':<24}{str(mcp_summary['mcp_acceptable_rate']):>10}",
            f"{'mcp_false_positive_rate':<24}{str(mcp_summary['mcp_false_positive_rate']):>10}",
            f"{'mcp_ambiguous_rate':<24}{str(mcp_summary['mcp_ambiguous_rate']):>10}",
            f"{'mcp_ambiguous_recall':<24}{str(mcp_summary['mcp_ambiguous_recall']):>10}",
            f"{'mcp_judge_picks':<24}{mcp_summary['mcp_judge_picks']:>10}",
            f"{'mcp_input_tokens/call':<24}{str(mcp_summary['mcp_input_tokens_per_call']):>10}",
            f"{'mcp_requests_per_call':<24}{str(mcp_summary['mcp_requests_per_call']):>10}",
            "",
            f"{'id':<30}{'hit1':>6}{'fp':>4}{'exists':>11}{'tokens':>9}  top1",
        ]
        for r in mcp_rows or []:
            lines.append(
                f"{r['id']:<30}{'y' if r['mcp_hit1'] else '-':>6}"
                f"{'y' if r['mcp_false_positive'] else '-':>4}"
                f"{str(r['exists']):>11}{str(r['input_tokens']):>9}  {r['top1_mcp']}"
            )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Routing accuracy/token harness for jev-engine.")
    ap.add_argument("--mode", choices=("mock", "live"), default="mock")
    ap.add_argument("--root", default=str(REPO), help="Workspace to search (default: this repo).")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ap.add_argument("--verbose", action="store_true", help="Print one JSON line per case.")
    ap.add_argument(
        "--only",
        choices=("all", "workspace", "mcp"),
        default="all",
        help="Which fixture set to run (default: all).",
    )
    args = ap.parse_args()

    os.environ["JEV_MCP_MOCK"] = "1" if args.mode == "mock" else "0"
    if args.mode == "live":
        from config import ensure_dotenv

        ensure_dotenv()
    config._reset_config_cache()
    jev_engine._reset_client_cache()
    jev_engine._reset_settings_cache()

    if args.mode == "live" and not os.getenv("TYPESAFE_API_KEY"):
        print("live mode needs TYPESAFE_API_KEY (or a repo .env)", file=sys.stderr)
        return 2

    summary, rows = evaluate(args.root, args.mode, verbose=args.verbose)
    mcp_summary, mcp_rows = (None, None)
    if args.only in ("all", "mcp"):
        mcp_summary, mcp_rows = evaluate_mcp(args.mode, verbose=args.verbose)
        summary = dict(summary, **mcp_summary)
    if args.only == "mcp":
        summary, rows = mcp_summary, mcp_rows
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows, "mcp_rows": mcp_rows}, indent=2))
    else:
        print(format_table(summary, rows, mcp_summary, mcp_rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
