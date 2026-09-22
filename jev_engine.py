import os
import sys
import json
from pathlib import Path
from typing import Dict, List, Any, Optional

# Load .env if present
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    else:
        load_dotenv(override=True)
except ImportError:
    pass

# Direct imports from the active virtual environment SDK
from typesafe_sdk import TypeSafeClient, Choice, Noul, Score

DEFAULT_SCAN_PATHS = [
    ".agents/skills",
    ".agents/workflows",
    ".agents/memory",
    ".opencode/skills",
    "skills",
    ".agents",
]


def _find_config_files() -> List[Path]:
    """Locate opencode.json candidates: project-level first, user-level last."""
    candidates = []
    for name in ("opencode.json", ".opencode/opencode.json"):
        local = Path.cwd() / name
        if local.exists():
            candidates.append(local)
    user = Path.home() / ".config" / "opencode" / "opencode.json"
    if user.exists():
        candidates.append(user)
    return candidates


def load_jev_settings() -> dict:
    """Read jev_settings from opencode.json (project wins over user)."""
    settings = {
        "enable_model_routing": False,
        "models": {},
        "scan_paths": list(DEFAULT_SCAN_PATHS),
    }
    for cfg in _find_config_files():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        jev = raw.get("jev_settings") or {}
        if "enable_model_routing" not in jev and not jev.get("models") and not jev.get("scan_paths"):
            continue
        if isinstance(jev.get("enable_model_routing"), bool):
            settings["enable_model_routing"] = jev["enable_model_routing"]
        models = jev.get("models") or {}
        if isinstance(models, dict):
            settings["models"] = {k: v for k, v in models.items() if v}
        scan_paths = jev.get("scan_paths")
        if isinstance(scan_paths, list):
            extras = [str(p) for p in scan_paths if isinstance(p, str)]
            settings["scan_paths"] = list(dict.fromkeys(list(DEFAULT_SCAN_PATHS) + extras))
        break
    return settings


def get_scan_paths(root: Path) -> List[Path]:
    """Return resolved scan directories (defaults + configured extras, deduped)."""
    settings = load_jev_settings()
    seen, paths = set(), []
    for rel in settings["scan_paths"]:
        p = (root / rel).resolve()
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            paths.append(p)
    return paths


def get_client() -> TypeSafeClient:
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        raise ValueError("TYPESAFE_API_KEY environment variable is not configured.")
    return TypeSafeClient(api_key=api_key)


def execute_system_one(client: TypeSafeClient, state: Any, questions: dict) -> Any:
    if hasattr(client, "system_one"):
        return client.system_one(state=state, questions=questions)
    elif hasattr(client, "decide"):
        return client.decide(state=state, decisions=questions)
    raise AttributeError("Configured client does not support system_one or decide")


def get_answer(response: Any, key: str) -> Any:
    if hasattr(response, "answers") and isinstance(response.answers, dict):
        return response.answers.get(key)
    return getattr(response, key, None)


def get_val(ans_obj: Any) -> Any:
    if ans_obj is None:
        return None
    keys = ["choice", "value", "key", "selected", "noul", "score"]
    if isinstance(ans_obj, dict):
        for attr in keys:
            v = ans_obj.get(attr)
            if v is not None:
                return v
        return ans_obj
    for attr in keys:
        v = getattr(ans_obj, attr, None)
        if v is not None:
            return v
    return ans_obj


def get_prob(ans_obj: Any) -> float:
    if ans_obj is None:
        return 0.0
    keys = ["noul", "probability", "confidence", "value", "score"]
    if isinstance(ans_obj, dict):
        for attr in keys:
            v = ans_obj.get(attr)
            if isinstance(v, (int, float)):
                return float(v)
        return 0.0
    for attr in keys:
        v = getattr(ans_obj, attr, None)
        if isinstance(v, (int, float)):
            return float(v)
    if isinstance(ans_obj, (int, float)):
        return float(ans_obj)
    return 0.0


# ----------------------------------------------------------------------
# 1. Command Verification Guardrail
# ----------------------------------------------------------------------
def verify_command(command: str) -> dict:
    client = get_client()
    state = f"Terminal shell command to execute: {command}"
    questions = {
        "is_destructive": Noul(
            instructions="Does this command permanently delete files, drop tables, or wipe directories?"
        ),
        "modifies_git": Noul(
            instructions="Does this command modify or delete git configuration or history (e.g. force push, rm -rf .git)?"
        )
    }

    res = execute_system_one(client, state=state, questions=questions)
    dest_prob = round(get_prob(get_answer(res, "is_destructive")), 2)
    git_prob = round(get_prob(get_answer(res, "modifies_git")), 2)

    is_safe = (dest_prob < 0.20) and (git_prob < 0.20)
    return {
        "safe": is_safe,
        "destructive_prob": dest_prob,
        "git_modify_prob": git_prob
    }


# ----------------------------------------------------------------------
# 2. Agent Resource & Skill Finder (.agents & .opencode)
# ----------------------------------------------------------------------
def find_agent_resources(task: str, root_dir: str = ".", max_matches: int = 5) -> dict:
    root = Path(root_dir).resolve()
    search_dirs = get_scan_paths(root)

    candidate_files: Dict[str, Path] = {}
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for p in sdir.rglob("*.md"):
            if p.is_file():
                rel = p.relative_to(root).as_posix()
                candidate_files[rel] = p

    if not candidate_files:
        return {
            "matched": False,
            "count": 0,
            "resources": [],
            "summary": "No Markdown resources or skills found in candidate directories."
        }

    options = list(candidate_files.keys())[:250]
    client = get_client()

    criteria_map = {opt: f"Agent resource: {Path(opt).name}" for opt in options}
    questions = {
        "primary": Choice(
            criteria=criteria_map,
            instructions="Select the primary matching agent skill, workflow, or memory document."
        ),
    }

    if len(options) > 1:
        secondary_map = {**criteria_map, "none": "No additional relevant resource"}
        questions["secondary"] = Choice(
            criteria=secondary_map,
            instructions="Select a secondary relevant skill or workflow, or choose 'none'."
        )
    if len(options) > 2:
        tertiary_map = {**criteria_map, "none": "No additional relevant resource"}
        questions["tertiary"] = Choice(
            criteria=tertiary_map,
            instructions="Select a third relevant skill or workflow, or choose 'none'."
        )

    res = execute_system_one(
        client,
        state=f"User Task: {task}\nGoal: Identify which specific Markdown agent resources are directly relevant.",
        questions=questions
    )

    selected_keys = []
    primary_ans = get_answer(res, "primary")
    primary_val = get_val(primary_ans)
    if primary_val and primary_val in candidate_files:
        selected_keys.append(primary_val)

    probs = getattr(primary_ans, "probabilities", {}) or {}
    if isinstance(probs, dict):
        for opt, p in probs.items():
            if opt in candidate_files and opt not in selected_keys and p >= 0.12:
                selected_keys.append(opt)

    for slot in ["secondary", "tertiary"]:
        val = get_val(get_answer(res, slot))
        if val and val != "none" and val in candidate_files and val not in selected_keys:
            selected_keys.append(val)

    if primary_val:
        primary_name = Path(primary_val).parent.name
        if "-" in primary_name:
            family_prefix = primary_name.rsplit("-", 1)[0] + "-"
            for opt in options:
                if opt not in selected_keys and family_prefix in opt:
                    selected_keys.append(opt)

    selected_keys = selected_keys[:max_matches]

    resources = []
    for rel_path in selected_keys:
        full_path = candidate_files[rel_path]
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(6000)
        except Exception:
            content = ""

        name = full_path.parent.name if full_path.name.lower() == "skill.md" else full_path.stem
        resources.append({
            "name": name,
            "file": rel_path,
            "content": content
        })

    summary_names = ", ".join(r["name"] for r in resources)
    return {
        "matched": len(resources) > 0,
        "count": len(resources),
        "primary": resources[0] if resources else None,
        "file": resources[0]["file"] if resources else None,
        "content": resources[0]["content"] if resources else None,
        "resources": resources,
        "summary": f"Found {len(resources)} relevant agent resource(s): {summary_names}"
    }


# ----------------------------------------------------------------------
# 3. Fast Workspace File Selector
# ----------------------------------------------------------------------
def select_target_files(task: str, root_dir: str = ".", max_results: int = 5) -> dict:
    root = Path(root_dir).resolve()
    ignore_dirs = {".git", ".godot", ".import", ".venv", "node_modules", "dist", "build"}
    ignore_exts = {".png", ".jpg", ".jpeg", ".webp", ".wav", ".ogg", ".mp3", ".ttf", ".import", ".zip"}

    candidates = []
    for p in root.rglob("*"):
        if any(ignored in p.parts for ignored in ignore_dirs):
            continue
        if p.is_file() and p.suffix.lower() not in ignore_exts:
            candidates.append(p.relative_to(root).as_posix())
        if len(candidates) >= 250:
            break

    if not candidates:
        return {"matched": False, "files": []}

    client = get_client()
    res = execute_system_one(
        client,
        state=f"User Task: {task}\nGoal: Identify which specific workspace files must be inspected or edited.",
        questions={
            "target_file": Choice(
                criteria={cand: "Candidate workspace file relevant to the task" for cand in candidates},
                instructions="Select the primary workspace file that directly relates to this task."
            )
        }
    )

    chosen = get_val(get_answer(res, "target_file"))
    return {
        "matched": bool(chosen),
        "files": [chosen] if chosen else []
    }


# ----------------------------------------------------------------------
# 4. CLI Entry Point
# ----------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: python jev_engine.py [verify|resource|files] [args...]"}))
        sys.exit(1)

    action = sys.argv[1].lower()
    if action == "verify":
        print(json.dumps(verify_command(sys.argv[2])))
    elif action in ["resource", "find_resource"]:
        root_path = sys.argv[3] if len(sys.argv) > 3 else "."
        print(json.dumps(find_agent_resources(sys.argv[2], root_path)))
    elif action in ["files", "target_files"]:
        root_path = sys.argv[3] if len(sys.argv) > 3 else "."
        print(json.dumps(select_target_files(sys.argv[2], root_path)))
    else:
        print(json.dumps({"error": f"Unknown action: {action}"}))
        sys.exit(1)