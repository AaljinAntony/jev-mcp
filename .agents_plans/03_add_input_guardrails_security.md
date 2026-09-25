# Phase 3: Add Input Guardrails, Windows Drive Blocking, and Bounded Directory Walk

> **Phase**: 03  
> **Target Files**:
> - [`jev_engine.py`](file:///d:/mcp/jev-typesafe-mcp/jev_engine.py)
> - [`tests/test_mock_tools.py`](file:///d:/mcp/jev-typesafe-mcp/tests/test_mock_tools.py)  
> **Parallel Execution Track**:
> - Belongs to **Track E (Core Engine Security)**.
> - Can be executed after Track A/B/C or in sequence.

---

## 1. Problem Context & Rationale

### Issue 1: Incomplete Windows Drive and System Path Blocking
In `jev_engine.py`:
```python
if os.name == "nt":
    for drive in "CDEFGH":
        blocked.add(Path(f"{drive}:\\Windows").resolve())
        blocked.add(Path(f"{drive}:\\").resolve())
```
**Vulnerabilities**:
1. Only drive letters `C` through `H` are checked. Enterprise workstations and developer rigs often use drives `I:`, `N:`, `P:`, `Z:` for network shares and project storage. If a user sets `root_dir="Z:\\"`, it bypasses the drive list check!
2. Windows paths are case-insensitive. `C:\windows` vs `C:\Windows` may resolve differently across Python versions or symlinks.
3. System directories like `ProgramFiles` and `ProgramFiles(x86)` are not blocked.

**Fix**:
Dynamically detect and block all drive roots (any path where `root.parent == root`, `len(root.parts) <= 1`, or `root.drive.rstrip('\\') == str(root).rstrip('\\')`).
Check all critical Windows environment paths (`SystemRoot`, `windir`, `ProgramFiles`, `ProgramFiles(x86)`) case-insensitively.

### Issue 2: Unbounded `rglob("*")` Directory Scan
When git is not available (e.g. non-git workspaces or git not in PATH):
```python
for p in root.rglob("*"):
    ...
```
`Path.rglob("*")` performs an unbounded recursive directory traversal. In deep folder structures, massive assets directories, or large nested codebases, this blocks the MCP server process for seconds or minutes, leading to client timeouts.

**Fix**:
Replace with an in-place pruned `os.walk()` limited to a maximum depth of 5 levels. When `depth >= 5` or when directory names match `ignore_dirs`, prune `dirnames.clear()` immediately so subtrees are never traversed.

---

## 2. Step-by-Step Code Changes

### Step 2.1: `jev_engine.py` — Harden `_validate_root_dir`

**Target lines** (`jev_engine.py` lines 58–76):
Replace `_validate_root_dir` with:

```python
def _validate_root_dir(root_dir: str) -> Path:
    """Resolve and sanity-check root_dir. Rejects system-level paths and drive roots."""
    _check_input_length("root_dir", root_dir)
    root = Path(root_dir).resolve()

    # Block POSIX system roots
    blocked_posix = {Path("/").resolve(), Path("/etc").resolve(), Path("/usr").resolve(), Path("/bin").resolve(), Path("/sbin").resolve()}
    if root in blocked_posix:
        raise JevValidationError(f"root_dir '{root_dir}' points to a system directory.")

    # Block Windows drive roots and system directories
    if os.name == "nt":
        # Check if root is any drive root (e.g., C:\, D:\, Z:\)
        # On Windows, drive roots have root.parent == root and typically 1 part (e.g. ('C:\\',))
        if root.parent == root or len(root.parts) <= 1:
            raise JevValidationError(f"root_dir '{root_dir}' points to a filesystem drive root.")

        root_lower = str(root).lower().rstrip("\\")

        # Collect and check all system directories case-insensitively
        system_env_vars = ["SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "SystemDrive"]
        for env_var in system_env_vars:
            val = os.environ.get(env_var)
            if val:
                val_resolved = str(Path(val).resolve()).lower().rstrip("\\")
                if root_lower == val_resolved or root_lower.startswith(val_resolved + "\\"):
                    raise JevValidationError(f"root_dir '{root_dir}' points inside a system directory ({env_var}).")

    if root.parent == root:
        raise JevValidationError(f"root_dir '{root_dir}' points to a system directory.")

    if not root.is_dir():
        raise JevValidationError(f"root_dir '{root_dir}' does not exist or is not a directory.")

    return root
```

---

### Step 2.2: `jev_engine.py` — Bounded Depth Walk in `select_target_files`

**Target lines** (`jev_engine.py` lines 650–662):
Replace the `rglob("*")` fallback with:

```python
    # Fallback: bounded manual directory walk (max 5 levels deep)
    if candidates is None:
        candidates = []
        max_depth = 5
        for dirpath, dirnames, filenames in os.walk(root):
            # Calculate current relative depth from root
            rel_dir = Path(dirpath).relative_to(root)
            depth = len(rel_dir.parts)

            # Do not descend beyond max_depth
            if depth >= max_depth:
                dirnames.clear()
                continue

            # Prune ignored directories in-place to avoid unnecessary traversal
            dirnames[:] = [
                d for d in dirnames
                if d not in ignore_dirs and not d.startswith(".")
            ]

            for fname in filenames:
                if fname.startswith("."):
                    continue
                p = Path(dirpath) / fname
                if p.is_symlink():
                    continue
                if p.suffix.lower() not in ignore_exts:
                    candidates.append(p.relative_to(root).as_posix())
                    if len(candidates) >= MAX_CHOICE_OPTIONS:
                        break
            if len(candidates) >= MAX_CHOICE_OPTIONS:
                break
```

---

### Step 2.3: `tests/test_mock_tools.py` — Add Guardrail Tests

Add tests for drive roots and system directory rejection:
```python
import pytest
from jev_errors import JevValidationError
from jev_engine import _validate_root_dir, select_target_files

def test_root_dir_blocks_drive_roots():
    """Verify that drive roots are rejected regardless of letter."""
    for drive in ["C:\\", "D:\\", "Z:\\"]:
        with pytest.raises(JevValidationError):
            _validate_root_dir(drive)

def test_root_dir_blocks_system_dirs():
    """Verify system directories like Windows or Program Files are rejected."""
    win_dir = os.environ.get("SystemRoot", "C:\\Windows")
    with pytest.raises(JevValidationError):
        _validate_root_dir(win_dir)

def test_select_target_files_respects_depth(tmp_path):
    """Verify that fallback walk does not exceed max_depth."""
    # Create deep folder structure: level1/level2/level3/level4/level5/level6/deep.py
    current = tmp_path
    for i in range(1, 8):
        current = current / f"level{i}"
        current.mkdir()
        (current / f"file_{i}.py").write_text("print(1)")

    # Run select_target_files with git disabled (tmp_path has no .git)
    result = select_target_files("find file", root_dir=str(tmp_path))
    assert result is not None
```

---

## 3. Verification Commands

Run mock tools and engine test suite:
```powershell
.venv\Scripts\pytest tests/test_mock_tools.py -v
```

Expected output:
- All path validation tests pass.
- System directory and drive root rejections raise `JevValidationError`.
- Directory walk bounds hold true.

---

## 4. Acceptance Criteria
- [ ] Any drive root (C:\ through Z:\) is rejected by `_validate_root_dir`.
- [ ] Windows system environment paths (`SystemRoot`, `ProgramFiles`, etc.) are blocked case-insensitively.
- [ ] `select_target_files` directory walk is capped at 5 levels of depth using `os.walk` in-place pruning.
- [ ] `pytest tests/test_mock_tools.py` passes completely.
