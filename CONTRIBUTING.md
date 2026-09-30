# Contributing

Thanks for looking at this. Two things make contributing here unusually easy:
**you do not need an API key**, and **the whole test suite is offline**.

## Set up

```bash
git clone https://github.com/AaljinAntony/jev-mcp.git
cd jev-mcp
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

```powershell
git clone https://github.com/AaljinAntony/jev-mcp.git
cd jev-mcp
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No `.env` is needed to run the tests. If you want to try the tools live, copy
`.env.example` to `.env` and add your key.

Check the install with `scripts/doctor.py` — read-only, no network.

## The dev loop

```bash
# Everything below runs offline.
.venv/bin/python -m pytest tests -q
node tests/test_plugin.mjs
.venv/bin/python scripts/bench_jev.py --assert
.venv/bin/python scripts/eval_routing.py --mode mock
```

| Command | What it does |
|---|---|
| `pytest tests -q` | 25 Python modules: validation, policy, limits, transport, mock tools, plugins |
| `node tests/test_plugin.mjs` | 16 sections driving the plugin against a real child process |
| `scripts/bench_jev.py --assert` | Performance gates. Exit `0` pass, `1` regression, `2` machine too loaded to judge |
| `scripts/eval_routing.py --mode mock` | Labelled routing quality over both fixture sets |
| `scripts/doctor.py` | Install health |
| `scripts/diag_mcp.py` | One real tool call over the real stdio transport |

Set `JEV_MCP_MOCK=1` to exercise the tools themselves without a key. That is
what `test_mock_tools.py` does.

`JEV_ROUTING_LIVE=1` (with a key) runs the live accuracy floors;
`JEV_MCP_LIVE=1` enables `test_live_smoke.py`. Neither is required for a green
PR.

CI runs the first three on Python 3.11–3.13 across Windows and Linux, with no
secrets — so it works on forks.

## Where things live

```
jev_mcp.py          the MCP server: 5 tools, error envelopes
jev_engine.py       the engine: settings, discovery, client, tool bodies, CLI
policy.py           confidence -> action, thresholds          (pure arithmetic)
jev_validation.py   fail-closed response validation
jev_errors.py       error taxonomy -> {code, message, retryable}
limits.py           every size cap, token estimation, truncation
candidates.py       file path -> short evidence for a Choice option
config.py           env parsing and validation
mock.py             deterministic offline judge
jev_logging.py      JSON-lines logging + credential redaction
scan_cache.py       path-only discovery cache
config/             sanitized config templates for every harness
docs/               ARCHITECTURE.md, perf-baseline.md
```

There is no package, no `pyproject.toml` and no `pytest.ini`. The modules are
flat and the entry points put the repo root on `sys.path` themselves. Please keep
it that way unless a PR specifically modernises the layout — it is a deliberate
choice for a repo you can clone and run in one command.

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) before a substantial change.

## Conventions

- **Fail closed.** If input is incomplete, truncated or invalid, the answer is
  `review` or an error. There is no path where bad input produces a confident
  good answer, and no PR should add one. See
  [SECURITY.md](SECURITY.md#3-fail-open-behaviour).
- **Validate before policy.** A response is validated against its questions
  *before* any probability is read. Do not reorder this to "save work" — it is
  the core safety property.
- **Every tool returns a plain `dict`** of `str` / `bool` / `float` / `None`.
  Never a pydantic object; the MCP layer serialises it.
- **Every deciding tool returns the same key set on every branch.** This is
  pinned by `tests/test_envelope_shapes.py`. Add a field to all branches or
  none, and update that test.
- **Bound everything an LLM supplies.** Input length, candidate counts, reads,
  tokens, wall clock. A new code path that reads the filesystem or waits on the
  network needs a cap in `limits.py`.
- **Respect the allowlist.** Any new path parameter is LLM-supplied and must go
  through the same containment check as `root_dir` — `Path.relative_to`, never
  `startswith`.
- **Docstrings explain *why*.** The existing modules are commented at the level
  of "this exists because that was a bug", not "increment i". Please match that;
  it is why the code is reviewable.
- **No comments that merely restate the code.** If the line says what it does,
  leave it out.

## The OpenCode plugin has its own rule

`config/jev-plugin.example.js` is a **copy**, not an import or a symlink. The
installed file at `~/.config/opencode/plugins/jev-plugin.js` is what OpenCode
actually loads, and drift is the failure mode.

1. Edit **`config/jev-plugin.example.js`**. Never the installed copy — an edit
   there is lost on the next reinstall and is invisible to review.
2. `node tests/test_plugin.mjs`. Section 16 byte-compares the example against
   the installed file and **fails** on any difference. Skips with a warning if
   the installed copy is absent.
3. `Copy-Item config\jev-plugin.example.js $env:USERPROFILE\.config\opencode\plugins\jev-plugin.js`
4. Restart OpenCode.

Treat the copy and the test as one step. `scripts/doctor.py` runs the same
SHA256 comparison for contributors without Node.

The plugin holds no Python and no TypeSafe SDK access — it drives the same
`jev_mcp.py` as an MCP client. Do not give it a second implementation of the
decision path. Section 13 asserts at the source level that the plugin contains
no `typesafe_sdk`, `TypeSafeClient`, `pythonScript`, `spawnSync` or
`TYPESAFE_API_KEY =`, and exactly one `spawn(`.

## Tests

Add a test with the change. The fixtures you will want:

- `tests/conftest.py::stub_choice` patches `jev_engine._request` — the single seam
  every tool funnels through — so you can supply a hand-built distribution while
  discovery, validation and policy still run for real.
- `tests/fixtures/routing_tasks.json` (25 labelled cases) and
  `tests/fixtures/mcp_selection_tasks.json` (13) drive
  `scripts/eval_routing.py`. Add a case here when you change how evidence is
  built or how a verdict is reached.

A test that needs network or a key does not belong in the default suite. Put it
behind `JEV_MCP_LIVE=1` or `JEV_ROUTING_LIVE=1`, the way
`test_live_smoke.py` is.

## Commits and pull requests

- Conventional-ish prefixes, matching the existing history:
  `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- One logical change per commit. A refactor and a behaviour change in the same
  commit is a review problem, not a style preference.
- Write the subject in the imperative, under ~70 characters, no trailing period.

In the PR description, say what changed, why, and how you verified it. If you
touched performance, include the `bench_jev.py --assert` result and note whether
the machine was idle. If you touched decision quality, include
`eval_routing.py` before-and-after numbers — a change that moves accuracy in
either direction is worth stating explicitly.

## Reporting bugs

Open an issue with the tool name, the input, the envelope you got, and what you
expected. A failing `pytest -k` or `node tests/test_plugin.mjs` output is ideal.
Security issues go through [SECURITY.md](SECURITY.md), not the issue tracker.

## Licence

Contributions are accepted under the [MIT licence](LICENSE). No CLA. Keep your
copyright on the lines you wrote; add yourself to the notices if you derived
something from a third party.