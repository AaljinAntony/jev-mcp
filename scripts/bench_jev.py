r"""Deterministic offline benchmark for the jev-engine hot paths.

Usage:
    & .\.venv\Scripts\python.exe scripts\bench_jev.py            # table
    & .\.venv\Scripts\python.exe scripts\bench_jev.py --json     # machine-readable
    & .\.venv\Scripts\python.exe scripts\bench_jev.py --assert   # CI regression gate

Every case is offline (JEV_MCP_MOCK=1) and seeds a synthetic workspace under
tempfile, so the numbers are reproducible across machines and do not depend on
the real repo's file count.
"""

import argparse
import json
import logging
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

# Ensure offline mock mode is active before loading config
os.environ["JEV_MCP_MOCK"] = "1"

ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config
import jev_engine
import jev_logging
import limits
import mock
from typesafe_sdk import Choice

# Reset module-level caches once at startup
config._reset_config_cache()
jev_engine._reset_client_cache()
jev_engine._reset_settings_cache()

# Silence informational round logs during benchmark execution
jev_logging.get_logger().setLevel(logging.WARNING)

# Regression thresholds: 1.25x the Phase 5 medians, which docs/perf-baseline.md
# records as ratios (Phase 1 = 1.00x). That document deliberately carries no
# absolute milliseconds — a wall-clock number measured here describes this host —
# so the literals live here, next to the gate that enforces them.
# Tighter than the 3x Phase 1 gates these replaced, so a regression is a visible
# failure rather than a footnote. These are the Phase 5 (idle) numbers; Phase 7
# re-measured every row on a loaded machine and did NOT move them, because the
# uniform inflation it saw - including on the pure-in-memory control row - was
# the machine. `IDLE_PROBE_MAX_MS` is what keeps a busy desktop from reading as a
# regression; see below.
THRESHOLDS = {
    "estimate_tokens_100k": 0.5,
    "estimate_tokens_750_options": 0.4,
    "fit_state_no_trunc": 0.5,
    "fit_state_trunc": 12.0,
    "mock_choice_250": 1.5,
    "mock_system_one_250": 3.0,
    # find_agent_resources reads a preview per candidate (Phase 3), so the cost
    # is dominated by 250 file reads rather than by discovery. Cold 114 ms (the
    # scan cache is cleared per run) vs 84 ms warm; the gap is the walk itself.
    "find_agent_resources_250": 145.0,
    "find_agent_resources_250_warm": 105.0,
    "select_target_files_git": 25.0,
    # The flat `file`/`content` duplicates are gone: 13.5 kB, 31% of the 44.2 kB
    # Phase 1 envelope. The gate leaves room for a slightly larger winning doc.
    "envelope_size_skills": 30_000,
}


def _run_case(fn, runs=5):
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        t1 = time.perf_counter()
        times.append((t1 - t0) * 1000.0)
    return round(statistics.median(times), 1), round(min(times), 1), round(max(times), 1)


def run_benchmarks(runs=5):
    results = []

    # 1. estimate_tokens_100k
    text_100k = ("The quick brown fox jumps over the lazy dog. " * 2500)[:100_000]
    med, mn, mx = _run_case(lambda: limits.estimate_tokens(text_100k), runs)
    results.append({
        "case": "estimate_tokens_100k",
        "median_ms": med,
        "min_ms": mn,
        "max_ms": mx,
        "bytes": None,
    })

    # 2. estimate_tokens_750_options
    criteria_250 = {
        f".agents/skills/skill_{i:03d}/SKILL.md": f"Agent resource: skill_{i:03d}.md"
        for i in range(250)
    }
    payload_dict = {
        "state": (
            "User Task: Refactor and optimize codebase\n"
            "Goal: Identify which specific Markdown agent resources are directly relevant."
        ),
        "questions": {
            "primary": {
                "criteria": criteria_250,
                "instructions": "Select the primary matching agent skill, workflow, or memory document.",
            },
            "secondary": {
                "criteria": {**criteria_250, "none": "No additional relevant resource"},
                "instructions": "Select a secondary relevant skill or workflow, or choose 'none'.",
            },
            "tertiary": {
                "criteria": {**criteria_250, "none": "No additional relevant resource"},
                "instructions": "Select a third relevant skill or workflow, or choose 'none'.",
            },
        },
    }
    payload_str = json.dumps(payload_dict)
    med, mn, mx = _run_case(lambda: limits.estimate_tokens(payload_str), runs)
    results.append({
        "case": "estimate_tokens_750_options",
        "median_ms": med,
        "min_ms": mn,
        "max_ms": mx,
        "bytes": None,
    })

    # 3. fit_state_no_trunc
    state_1k = ("Agent state message without truncation. " * 30)[:1_000]
    q_simple = {"primary": Choice(criteria={"opt1": "Option 1"}, instructions="Select one")}
    med, mn, mx = _run_case(lambda: limits.fit_state(state_1k, q_simple), runs)
    results.append({
        "case": "fit_state_no_trunc",
        "median_ms": med,
        "min_ms": mn,
        "max_ms": mx,
        "bytes": None,
    })

    # 4. fit_state_trunc
    state_200k = ("Large context state block for testing truncation behavior. " * 4000)[:200_000]
    med, mn, mx = _run_case(lambda: limits.fit_state(state_200k, q_simple), runs)
    results.append({
        "case": "fit_state_trunc",
        "median_ms": med,
        "min_ms": mn,
        "max_ms": mx,
        "bytes": None,
    })

    # 5. mock_choice_250
    state_choice = "Task: refactor python code and add type annotations to improve system reliability."
    choice_q = Choice(criteria=criteria_250, instructions="Select the primary matching agent skill.")
    med, mn, mx = _run_case(lambda: mock._mock_choice(state_choice, choice_q), runs)
    results.append({
        "case": "mock_choice_250",
        "median_ms": med,
        "min_ms": mn,
        "max_ms": mx,
        "bytes": None,
    })

    # 6. mock_system_one_250
    q_three = {
        "primary": Choice(criteria=criteria_250, instructions="Select the primary matching agent skill."),
        "secondary": Choice(criteria={**criteria_250, "none": "No additional relevant resource"}, instructions="Select secondary."),
        "tertiary": Choice(criteria={**criteria_250, "none": "No additional relevant resource"}, instructions="Select tertiary."),
    }
    med, mn, mx = _run_case(lambda: mock.mock_system_one(state_choice, q_three), runs)
    results.append({
        "case": "mock_system_one_250",
        "median_ms": med,
        "min_ms": mn,
        "max_ms": mx,
        "bytes": None,
    })

    # File-based benchmark cases with synthetic workspace
    with tempfile.TemporaryDirectory() as tmp_dir:
        # The synthetic workspace lives under the system temp dir, which is not
        # on the `root_dir` allowlist by default. Allowlisting the temp root is
        # exactly what JEV_MCP_ALLOWED_ROOTS is for.
        os.environ["JEV_MCP_ALLOWED_ROOTS"] = tempfile.gettempdir()
        config._reset_config_cache()

        # Pin the scan set to this synthetic workspace. The server also scans the
        # current user's global skill directories, so without this the numbers
        # below would measure whatever the machine running the benchmark has
        # installed — the thresholds were calibrated against 250 workspace
        # skills and nothing else.
        bench_home = Path(tmp_dir) / "bench-home"
        bench_home.mkdir(exist_ok=True)
        os.environ["USERPROFILE"] = str(bench_home)
        os.environ["HOME"] = str(bench_home)

        # Seed 250 .md skills with realistic group prefix and content
        skills_dir = Path(tmp_dir) / ".agents" / "skills"
        for i in range(250):
            d = skills_dir / f"group-{i:03d}"
            d.mkdir(parents=True, exist_ok=True)
            p = d / "SKILL.md"
            p.write_text(
                f"# Skill group {i:03d}\n"
                + ("Guidelines and instructions for automated agent skill.\n" * 150),
                encoding="utf-8",
            )

        # Seed non-git files for select_target_files (250 files across subdirs)
        src_dir = Path(tmp_dir) / "src"
        for d in ["components", "utils", "services", "models", "controllers"]:
            sub = src_dir / d
            sub.mkdir(parents=True, exist_ok=True)
            for i in range(50):
                (sub / f"module_{i:03d}.py").write_text(
                    f"# module {i}\ndef process_{i}(): pass\n",
                    encoding="utf-8",
                )

        # 7. find_agent_resources_250 — COLD: the scan cache is cleared before
        # every run, so this is the cost of discovering the tree again.
        last_skill_res = None
        def _call_find_resources_cold():
            nonlocal last_skill_res
            jev_engine._reset_scan_cache()
            last_skill_res = jev_engine.find_agent_resources(
                "group-001",
                root_dir=tmp_dir,
            )

        med, mn, mx = _run_case(_call_find_resources_cold, runs)
        res_bytes = len(json.dumps(last_skill_res)) if last_skill_res else 0
        results.append({
            "case": "find_agent_resources_250",
            "median_ms": med,
            "min_ms": mn,
            "max_ms": mx,
            "bytes": res_bytes,
        })

        # 8. find_agent_resources_250_warm — same request on a primed cache.
        jev_engine.find_agent_resources("group-001", root_dir=tmp_dir)
        med, mn, mx = _run_case(
            lambda: jev_engine.find_agent_resources("group-001", root_dir=tmp_dir),
            runs,
        )
        results.append({
            "case": "find_agent_resources_250_warm",
            "median_ms": med,
            "min_ms": mn,
            "max_ms": mx,
            "bytes": res_bytes,
        })

        # 9. select_target_files_git
        med, mn, mx = _run_case(
            lambda: jev_engine.select_target_files("implement feature in python modules", root_dir=tmp_dir),
            runs,
        )
        results.append({
            "case": "select_target_files_git",
            "median_ms": med,
            "min_ms": mn,
            "max_ms": mx,
            "bytes": None,
        })

        # 10. envelope_size_skills
        results.append({
            "case": "envelope_size_skills",
            "median_ms": None,
            "min_ms": None,
            "max_ms": None,
            "bytes": res_bytes,
        })

    return results


def format_table(results):
    lines = [
        f"{'case':<28}{'median_ms':>11}{'min_ms':>9}{'max_ms':>9}{'bytes':>8}"
    ]
    for r in results:
        case = r["case"]
        med_str = f"{r['median_ms']:.1f}" if r["median_ms"] is not None else "-"
        min_str = f"{r['min_ms']:.1f}" if r["min_ms"] is not None else "-"
        max_str = f"{r['max_ms']:.1f}" if r["max_ms"] is not None else "-"
        bytes_str = str(r["bytes"]) if r["bytes"] is not None else "-"
        lines.append(f"{case:<28}{med_str:>11}{min_str:>9}{max_str:>9}{bytes_str:>8}")
    return "\n".join(lines)


#: Idle-machine precondition for the wall-clock gates. These gates measure
#: elapsed time, so on a machine carrying unrelated work they report the
#: desktop's mood rather than this code's behaviour. `fit_state_trunc` is the
#: right probe for that: it is a pure in-memory benchmark that no change in this
#: project can move, and it is already in the table.
#:
#: Measured on this machine (docs/perf-baseline.md, Phase 7):
#:     idle                 4.9 ms   -> precondition passes, gates are meaningful
#:     20-30% foreign load  6.7-9.0 ms -> precondition fails
#:     40%+ foreign load    8.0-9.5 ms -> precondition fails
#:
#: 6.0 ms sits above the idle figure and below every loaded one. Above it,
#: `--assert` still enforces the size gates (bytes do not depend on load) and
#: reports the wall-clock gates as inconclusive. Exit codes: 0 pass,
#: 1 regression, 2 machine too loaded to judge.
IDLE_PROBE_CASE = "fit_state_trunc"
IDLE_PROBE_MAX_MS = 6.0


def main():
    parser = argparse.ArgumentParser(description="Deterministic offline benchmark for jev-engine hot paths.")
    parser.add_argument("--json", action="store_true", help="Output results as JSON.")
    parser.add_argument("--assert", dest="assert_mode", action="store_true", help="Assert results against performance thresholds.")
    parser.add_argument("--runs", type=int, default=5, help="Number of iterations per case (default: 5).")
    args = parser.parse_args()

    results = run_benchmarks(runs=args.runs)
    by_case = {r["case"]: r for r in results}

    probe_ms = by_case.get(IDLE_PROBE_CASE, {}).get("median_ms")
    loaded = probe_ms is not None and probe_ms > IDLE_PROBE_MAX_MS

    if args.json:
        print(json.dumps({"load_probe_case": IDLE_PROBE_CASE, "load_probe_ms": probe_ms,
                          "inconclusive": loaded, "results": results}, indent=2))
    else:
        print(format_table(results))
        if args.assert_mode and loaded:
            print(
                f"\nINCONCLUSIVE: {IDLE_PROBE_CASE} took {probe_ms:.1f} ms (> {IDLE_PROBE_MAX_MS} ms), "
                f"so this machine is too loaded for the wall-clock gates to mean anything. "
                f"The size gates are still enforced; re-run on an idle machine for the rest."
            )

    if args.assert_mode:
        failures = []
        for r in results:
            case = r["case"]
            threshold = THRESHOLDS.get(case)
            if threshold is None:
                continue
            if r["median_ms"] is not None:
                if r["median_ms"] > threshold:
                    kind = "Inconclusive" if loaded else "Regression"
                    suffix = (
                        f" (machine busy - see {IDLE_PROBE_CASE} above)"
                        if loaded
                        else ""
                    )
                    failures.append(
                        f"{kind} in {case}: median {r['median_ms']}ms > threshold {threshold}ms{suffix}"
                    )
            elif r["bytes"] is not None:
                # Load-independent: always enforced, even on a busy machine.
                if r["bytes"] > threshold:
                    failures.append(
                        f"Regression in {case}: bytes {r['bytes']} > threshold {threshold}"
                    )
        if failures:
            for msg in failures:
                sys.stderr.write(f"{'SKIP' if msg.startswith('Inconclusive') else 'FAIL'}: {msg}\n")
            sys.exit(2 if loaded else 1)
        else:
            if not args.json:
                print("\nAll assert gates passed.")
            if loaded:
                sys.exit(2)


if __name__ == "__main__":
    main()
