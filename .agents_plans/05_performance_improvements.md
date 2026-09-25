# Phase 5: Performance Improvements

> **Priority:** 🟡 High
> **Estimated effort:** ~1.5 hours
> **Files to modify:** `limits.py`, `jev_engine.py`

---

## Task 5A: Eliminate Binary-Search Token Estimation

### Problem

`truncate_to_token_budget()` in `limits.py` (lines 120–134) uses a binary search to find the longest prefix of a string that fits within a token budget. Each iteration calls `truncate_text()` + `estimate_tokens()`, which re-scans the candidate string character by character.

For a 256K character string, this runs ~18 binary search iterations, each scanning up to 128K+ characters. That's ~2.3 million character inspections for what should be a direct calculation.

### Where to look

- File: `limits.py`, lines 24–34 (`estimate_tokens()`) and lines 120–134 (`truncate_to_token_budget()`)

### Root cause

The `estimate_tokens()` function uses a simple formula: `ceil(ascii_chars / 4 + non_ascii_chars)`. This is a monotonically increasing function of string length. Given a token budget, we can directly compute the maximum character count without binary search.

### Exact fix

Replace the binary-search `truncate_to_token_budget()` with a direct character-budget calculation:

```python
def _char_budget_for_tokens(text: str, token_budget: int) -> int:
    """Compute the maximum character count that fits within a token budget.

    Since estimate_tokens uses ceil(ascii/4 + non_ascii), the worst case
    is all non-ASCII (1 token per char) and the best case is all ASCII
    (4 chars per token). We scan to find the exact cutoff.
    """
    if token_budget <= 0:
        return 0
    # Account for the truncation marker that will be appended
    marker_tokens = estimate_tokens(TRUNCATION_MARKER)
    available = token_budget - marker_tokens
    if available <= 0:
        return 0

    # Walk forward counting tokens consumed, stop when budget exhausted
    tokens_used = 0
    for i, char in enumerate(text):
        cost = 0.25 if ord(char) <= 0x7F else 1.0
        if tokens_used + cost > available:
            return i
        tokens_used += cost
    return len(text)


def truncate_to_token_budget(text: str, budget: int) -> str:
    """Truncate text to fit within the estimated token budget."""
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    max_chars = _char_budget_for_tokens(text, budget)
    return truncate_text(text, max_chars + len(TRUNCATION_MARKER))
```

### Why this is better

- **Single pass** through the text instead of ~18 passes
- **O(n)** instead of **O(n log n)** where n is the string length
- For a 256K string: ~256K char inspections vs ~2.3M
- In practice, the budget limit will stop the scan early (often after ~128K chars for a 32K token budget), so it's even faster

### Alternative simpler approach

If the above feels over-engineered, a simpler fix is to estimate the character budget directly from the token budget using the worst-case ratio:

```python
def truncate_to_token_budget(text: str, budget: int) -> str:
    """Truncate text to fit within the estimated token budget."""
    if budget <= 0:
        return ""
    if estimate_tokens(text) <= budget:
        return text
    # Worst case: all non-ASCII = 1 token/char. Best case: all ASCII = 4 chars/token.
    # Use 4× budget as initial char estimate (works for mostly-ASCII text),
    # then verify and trim if needed.
    char_limit = budget * 4
    candidate = truncate_text(text, char_limit)
    while estimate_tokens(candidate) > budget and char_limit > 0:
        char_limit = int(char_limit * 0.9)  # shrink by 10% each time
        candidate = truncate_text(text, char_limit)
    return candidate
```

This converges in 1–3 iterations for ASCII-heavy text (which is the vast majority of code/command state). Use this simpler approach if the single-pass scan feels like premature optimization.

### Validation

Run:
```bash
pytest tests/test_limits.py -v
```

All 4 existing limit tests must pass, especially:
- `test_huge_state_truncated` — verifies truncation works
- `test_state_stays_below_budget_after_fit` — verifies the result is within budget

---

## Task 5B: Use `git ls-files` for File Discovery

### Problem

`select_target_files()` in `jev_engine.py` (lines 523–535) uses `root.rglob("*")` to discover candidate files. On a large repository:
- This walks the entire directory tree, entering every non-ignored directory
- Even with `ignore_dirs` filtering, the walk still enters directories before checking
- The 250-candidate cap means deep files may be consistently missed
- This is called on every `search_target_files` tool invocation

### Where to look

- File: `jev_engine.py`, lines 523–535

### Current code

```python
candidates = []
for p in root.rglob("*"):
    if any(ignored in p.parts for ignored in ignore_dirs):
        continue
    if p.is_file() and p.suffix.lower() not in ignore_exts:
        candidates.append(p.relative_to(root).as_posix())
    if len(candidates) >= MAX_CHOICE_OPTIONS:
        break
```

### Exact fix

Try `git ls-files` first (fast, respects `.gitignore`), fall back to `rglob` for non-git repos:

```python
def _discover_files_git(root: Path, ignore_exts: set, max_count: int) -> Optional[List[str]]:
    """Use git ls-files for fast, .gitignore-aware file discovery. Returns None if not a git repo."""
    import subprocess
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        candidates = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if Path(line).suffix.lower() not in ignore_exts:
                candidates.append(line.replace("\\", "/"))
            if len(candidates) >= max_count:
                break
        return candidates
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def select_target_files(task: str, root_dir: str = ".", max_results: int = 5) -> dict:
    root = Path(root_dir).resolve()
    ignore_dirs = {".git", ".godot", ".import", ".venv", "node_modules", "dist", "build"}
    ignore_exts = {".png", ".jpg", ".jpeg", ".webp", ".wav", ".ogg", ".mp3", ".ttf", ".import", ".zip"}

    # Fast path: use git if available
    candidates = _discover_files_git(root, ignore_exts, MAX_CHOICE_OPTIONS)

    # Fallback: manual directory walk
    if candidates is None:
        candidates = []
        for p in root.rglob("*"):
            if any(ignored in p.parts for ignored in ignore_dirs):
                continue
            if p.is_file() and p.suffix.lower() not in ignore_exts:
                candidates.append(p.relative_to(root).as_posix())
            if len(candidates) >= MAX_CHOICE_OPTIONS:
                break

    if not candidates:
        return {"matched": False, "files": [], "exists": "absent"}

    # ... rest of the function remains exactly the same from the criteria dict onward
```

### Design decisions

- **`--cached --others --exclude-standard`**: Lists tracked files + untracked-but-not-ignored files. This gives a complete view of the working tree minus .gitignored files.
- **`timeout=5`**: Prevents hanging on broken git repos or network filesystems.
- **Returns `None` on failure**: Cleanly falls back to the existing `rglob` approach. No behavior change for non-git projects.
- **`FileNotFoundError` catch**: Handles systems where `git` is not installed.

### Performance impact

- **Git repo with 10K files:** `git ls-files` takes ~50ms vs `rglob("*")` which can take 500ms–2s
- **Non-git repo:** No change — falls back to existing behavior
- **Git not installed:** No change — falls back to existing behavior

### Validation

Run:
```bash
pytest tests/test_mock_tools.py::TestSearchTargetFiles -v
```

Tests use `tmp_path` (no `.git` directory) so they'll use the fallback path. The fix is safe.

For manual validation, run from a git repo:
```bash
python jev_engine.py files "find the config" .
```

---

## Task 5C: Reduce Redundant Serialization (Low Priority)

### Problem

In `limits.py`, `fit_state()` (lines 78–117):
1. `estimate_tokens(questions)` serializes the entire questions dict via `stringify_state()`
2. `longest_question_tokens(questions)` then re-serializes each individual question value

For a 250-option Choice question, the criteria dict is serialized twice — once as part of the full questions dict, once individually. This is ~200KB of redundant JSON serialization.

### Where to look

- File: `limits.py`, lines 86–91

### Current code

```python
questions_tokens = estimate_tokens(questions)
longest_tokens = longest_question_tokens(questions)
budget = min(
    MAX_TOTAL_TOKENS - questions_tokens,
    MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS - longest_tokens,
)
```

### Exact fix

Pre-serialize each question once, then sum/max over the pre-computed values:

```python
def fit_state(state, questions) -> dict:
    # Pre-serialize each question once
    question_token_counts = {}
    if questions and hasattr(questions, "items"):
        for key, value in questions.items():
            question_token_counts[key] = estimate_tokens(value)

    questions_tokens = sum(question_token_counts.values()) if question_token_counts else estimate_tokens(questions)
    longest_tokens = max(question_token_counts.values()) if question_token_counts else 0

    budget = min(
        MAX_TOTAL_TOKENS - questions_tokens,
        MAX_STATE_PLUS_LONGEST_QUESTION_TOKENS - longest_tokens,
    )
    # ... rest unchanged
```

### Caveat

The total `questions_tokens` from summing individual question estimates will be slightly different from estimating the full dict at once (because the full dict includes the key names and JSON structure). The difference is negligible for budget estimation purposes (~tens of tokens vs a 64K budget).

If exact parity is needed, keep the original `estimate_tokens(questions)` call for `questions_tokens` and only optimize `longest_question_tokens`:

```python
questions_tokens = estimate_tokens(questions)
longest_tokens = max(
    (estimate_tokens(v) for v in questions.values()),
    default=0,
) if questions and hasattr(questions, "items") else 0
```

This eliminates the `longest_question_tokens()` function call and its redundant `hasattr` check.

---

## Checklist

- [x] `limits.py`: Replace `truncate_to_token_budget()` with direct character-budget calculation (or simpler iterative approach)
- [x] `jev_engine.py`: Add `_discover_files_git()` helper function
- [x] `jev_engine.py`: Update `select_target_files()` to try git first, fall back to rglob
- [x] `limits.py`: Optimize `fit_state()` to avoid redundant question serialization
- [x] Run `pytest tests/test_limits.py -v` — all tests green
- [x] Run `pytest tests/test_mock_tools.py -v` — all tests green
