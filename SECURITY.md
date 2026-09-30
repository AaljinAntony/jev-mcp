# Security policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue.

- Use GitHub's **Security → Report a vulnerability** on this repository, or
- Open a private issue, or

Include:

- what you did, step by step;
- what you expected and what happened instead;
- the version or commit;
- any input needed to reproduce — a **redacted** command, task string or roster.
  Never paste a live `TYPESAFE_API_KEY`.

Expect an acknowledgement within a week. Fixes land as fast as they reasonably
can; critical issues (credential exposure, allowlist escape) are prioritised
over everything else.

## What counts as a security bug here

This server runs inside an agent's session, holds an API key, and takes paths and
prose from a model. The findings that matter:

### 1. Credential exposure

- An API key reaching a log file, a traceback, an error envelope or a tool
  result in a form that is not redacted.
- A harness config or `.env` being read from a path the caller did not authorise.
- A key appearing in a committed file.

### 2. Filesystem escape

- `root_dir` or `task_file` resolving **outside** the allowed roots.
- The allowlist being bypassable with `..`, a symlink or junction, a
  sibling-prefix path (`<root>-evil`), an absolute path, or a drive root.
- Either of the two stop conditions in the ancestor walk failing to hold: the
  walk must never reach the filesystem root, because `_is_within` uses
  `relative_to` and a base of `/` makes every absolute path a member; and it
  must never reach `$HOME` or above. A regression that reintroduces either one
  exposes the whole filesystem, or the user's home directory, to an
  LLM-supplied `root_dir`. Neither is visible from a checkout on a separate
  Windows volume, so treat a POSIX test failure here as a real finding.
- A `scan_paths` entry from `jevs_settings.json` escaping the workspace in the
  OpenCode plugin - that file is documented as safe to commit and share, so it
  is untrusted input.
- Any path reaching an OS system directory (`C:\Windows`, `C:\Program Files`, a
  filesystem root; `/etc`, `/proc`, `/sys`, `/dev`, `/var`, `/opt`, `/srv`,
  `/root`).

### 3. Fail-open behaviour

The single most important property of this project: **an invalid or
incomplete answer must never read as a good one.** Report it if you can find a
path where:

- `guardrail_command` returns `safe: true` from a response that failed
  validation, a truncated state, or a `NaN`/`inf` anywhere in the payload;
- a truncated context, a truncated candidate set, or an over-limit tool list
  yields `action: "auto"`;
- a key-set difference between two branches of a tool lets a caller read a
  verdict that was never computed;
- a malformed response reaches the policy layer at all.

### 4. Resource exhaustion

- An LLM-supplied input that causes unbounded filesystem reads, unbounded
  memory, or an unbounded wait.
- A way to make the circuit breaker permanently open and deny service.
- A way to make the `select_mcp_tools` self-exclusion fail so the judge is
  offered as its own candidate.

### 5. Plugin isolation

- An exception in `config/jev-plugin.example.js` propagating into an OpenCode
  message hook and crashing the session.
- The plugin writing prompt text to its log.
- The plugin reading the API key itself rather than letting the server do it.

## What is **not** a vulnerability

Being clear about this saves time in both directions.

- **`guardrail_command` is advisory.** It judges; it cannot block a tool call.
  A command the judge calls `safe: true` that turns out to be destructive is a
  model limitation, not a bug. The README says so explicitly. Enforcement
  belongs to the host's permission system.
- **`JEV_MCP_MOCK=1` produces poor decisions.** It is a deterministic offline
  stand-in for tests and demos. It is documented as such.
- **A wrong answer with a high confidence.** Jev is a model. It is stochastic and
  it will be wrong sometimes. Report *systematically* biased or fail-open
  behaviour, not individual wrong calls. `tests/fixtures/` and
  `scripts/eval_routing.py` exist to measure that.
- **Wall-clock gate failures on a loaded machine.** `bench_jev.py --assert`
  reports `INCONCLUSIVE` and exits `2` for exactly this. Exit `1` on a busy
  desktop is the known limitation documented in
  [docs/perf-baseline.md](docs/perf-baseline.md), not a regression.
- **The server reading files inside the workspace.** That is its job. It is
  bounded by the caps in `limits.py` and confined by the allowlist.

## Handling secrets

**If you commit a real `TYPESAFE_API_KEY`, rotate it.** Treat it as compromised
the moment it lands in a public repository — assume it was scraped.

```bash
# If it was only in the last unpushed commit
git reset --soft HEAD~1        # then re-commit without it

# Then, always
# 1. rotate the key in the TypeSafe dashboard
# 2. purge it from history if it was pushed:
#    git filter-repo --invert-paths --path .env
```

The repository's `.gitignore` blocks `.env`, `.env.*`, `opencode.json`,
`.opencode/`, `.mcp.json`, `.cursor/`, `.codex/`, `.hermes/`, `*.local.json`,
`secrets/`, `*.pem` and `*.key`. If you add a new place a secret could live, add
it there in the same PR.

For defence in depth, the server redacts credential-shaped strings from tool
arguments, error messages and log previews — but redaction is a backstop, not a
substitute for not writing secrets down.