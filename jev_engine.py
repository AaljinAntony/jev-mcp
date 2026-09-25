import os
import sys
import json
import time
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
from typesafe_sdk import TypeSafeClient, Choice, Noul, Score, RetryPolicy
from typesafe_sdk import (
    TypeSafeAPIConnectionError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
)

from config import get_config
from jev_errors import JevConfigError, JevResponseError, JevTimeoutError, error_details
from jev_logging import log_round
from jev_validation import validate_response
from limits import fit_state, MAX_CHOICE_OPTIONS, MAX_CONTENT_CHARS
from mock import mock_system_one
from policy import (
    action_from_confidence,
    confidence_from_probabilities,
    guardrail_safe,
    min_confidence,
    require_complete_context,
    worst_action,
)

DEFAULT_SCAN_PATHS = [
    ".agents/skills",
    ".agents/workflows",
    ".agents/memory",
    ".opencode/skills",
    "skills",
    ".agents",
]


def _find_settings_files() -> List[Path]:
    """Locate jevs_settings.json candidates: project-level first, user-level last."""
    candidates = []
    for name in ("jevs_settings.json", ".opencode/jevs_settings.json"):
        local = Path.cwd() / name
        if local.exists():
            candidates.append(local)
    user = Path.home() / ".config" / "opencode" / "jevs_settings.json"
    if user.exists():
        candidates.append(user)
    return candidates


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


def _apply_jev_settings(settings: dict, jev: dict) -> bool:
    """Merge a raw jev_settings dict onto `settings`. Returns True if it matched."""
    jev = jev or {}
    if "enable_model_routing" not in jev and not jev.get("models") and not jev.get("scan_paths"):
        return False
    if isinstance(jev.get("enable_model_routing"), bool):
        settings["enable_model_routing"] = jev["enable_model_routing"]
    models = jev.get("models") or {}
    if isinstance(models, dict):
        settings["models"] = {k: v for k, v in models.items() if v}
    scan_paths = jev.get("scan_paths")
    if isinstance(scan_paths, list):
        extras = [str(p) for p in scan_paths if isinstance(p, str)]
        settings["scan_paths"] = list(dict.fromkeys(list(DEFAULT_SCAN_PATHS) + extras))
    return True


def load_jev_settings() -> dict:
    """Read Jev settings from jevs_settings.json (project wins over user).

    Lookup order: <cwd>/jevs_settings.json -> <cwd>/.opencode/jevs_settings.json ->
    ~/.config/opencode/jevs_settings.json -> legacy `jev_settings` block in
    opencode.json (project > user). Falls back to defaults when nothing matches:
    routing off, empty model map, built-in scan paths.
    """
    settings = {
        "enable_model_routing": False,
        "models": {},
        "scan_paths": list(DEFAULT_SCAN_PATHS),
        "source": None,
    }
    for cfg in _find_settings_files():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if _apply_jev_settings(settings, raw.get("jev_settings") or raw):
            settings["source"] = str(cfg)
            return settings
    for cfg in _find_config_files():
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception as e:
            sys.stderr.write(f"jev_engine: failed reading {cfg}: {e}\n")
            continue
        if _apply_jev_settings(settings, raw.get("jev_settings") or {}):
            settings["source"] = str(cfg)
            return settings
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


def get_client() -> Optional[TypeSafeClient]:
    """Return a configured TypeSafe client, or None in mock mode.

    Raises `JevConfigError` when `TYPESAFE_API_KEY` is missing (unless
    `JEV_MCP_MOCK=1`, which never needs a key).
    """
    cfg = get_config()
    if cfg.mock:
        return None
    if not cfg.api_key:
        raise JevConfigError("TYPESAFE_API_KEY environment variable is not configured.")
    return TypeSafeClient(
        api_key=cfg.api_key,
        timeout=cfg.timeout_ms / 1000.0,
        retry=RetryPolicy(
            max_retries=2,
            backoff_initial=0.5,
            backoff_max=5.0,
            backoff_jitter=0.25,
        ),
    )


def execute_system_one(client, state: Any, questions: dict) -> Any:
    """Run `system_one`, re-raising SDK failures as typed Jev errors.

    In mock mode (or with a None client) a deterministic offline judge answers,
    so the server and tests run without a network round-trip.
    """
    cfg = get_config()
    model = cfg.model or None
    if cfg.mock or client is None:
        return mock_system_one(state, questions, model=model or "jev-latest")
    try:
        if hasattr(client, "system_one"):
            return client.system_one(state=state, questions=questions, model=model)
        elif hasattr(client, "decide"):
            return client.decide(state=state, decisions=questions, model=model)
        raise JevConfigError("Configured client does not support system_one or decide")
    except TypeSafeAPITimeoutError as e:
        raise JevTimeoutError() from e
    except TypeSafeAPIConnectionError as e:
        raise JevTimeoutError("Could not connect to TypeSafe.") from e
    except TypeSafeAPIResponseValidationError as e:
        raise JevResponseError() from e


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
# Result / envelope helpers
# ----------------------------------------------------------------------
def _attr_opt(obj: Any, name: str):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _usage_dict(usage: Any) -> Optional[dict]:
    if usage is None:
        return {"input_tokens": None, "output_tokens": None}
    return {
        "input_tokens": _attr_opt(usage, "input_tokens"),
        "output_tokens": _attr_opt(usage, "output_tokens"),
    }


def _response_meta(res: Any, cfg) -> dict:
    """Shared `model`, `usage` envelope from a validated response."""
    return {
        "model": getattr(res, "model", None) or cfg.model or None,
        "usage": _usage_dict(_attr_opt(res, "usage")),
    }


def _slot_confidence(ans_obj: Any) -> float:
    """Confidence of a single answer; falls back to the distribution."""
    if ans_obj is None:
        return 0.0
    conf = _attr_opt(ans_obj, "confidence")
    if isinstance(conf, (int, float)):
        return float(conf)
    probs = _attr_opt(ans_obj, "probabilities")
    if isinstance(probs, dict) and probs:
        return confidence_from_probabilities(probs)
    prob = get_prob(ans_obj)
    if prob:
        return max(prob, 1.0 - prob)
    return 0.0


def _noul_confidence(prob: float) -> float:
    return max(prob, 1.0 - prob)


def _risk_action(prob: float, cfg) -> str:
    """Guardrail action from a risk noul.

    A confident ``destructive_prob >= 0.5`` never runs on its own, and the
    0.20 boundary matches the backward-compatible ``safe`` thresholds.
    """
    if prob >= 0.50:
        return "escalate"
    if prob >= 0.20:
        return "review"
    return action_from_confidence(_noul_confidence(prob), cfg.auto_accept, cfg.review_at)


def _request(state_text: str, questions: dict):
    """Fit + execute + validate one Jev round. Returns (res, fitted)."""
    cfg = get_config()
    started = time.perf_counter()
    try:
        fitted = fit_state(state_text, questions)
        res = execute_system_one(get_client(), state=fitted["state"], questions=questions)
        validate_response(res, questions)
        log_round(
            (time.perf_counter() - started) * 1000,
            question_keys=questions.keys(),
            state_len=len(fitted["state"]),
        )
        return res, fitted, cfg
    except Exception as err:
        log_round(
            (time.perf_counter() - started) * 1000,
            question_keys=questions.keys(),
            state_len=len(state_text),
            error=err,
        )
        raise


# ----------------------------------------------------------------------
# 1. Command Verification Guardrail
# ----------------------------------------------------------------------
def verify_command(command: str) -> dict:
    state = f"Terminal shell command to execute: {command}"
    questions = {
        "is_destructive": Noul(
            instructions="Does this command permanently delete files, drop tables, or wipe directories?"
        ),
        "modifies_git": Noul(
            instructions="Does this command modify or delete git configuration or history (e.g. force push, rm -rf .git)?"
        )
    }

    res, fitted, cfg = _request(state, questions)

    dest_prob = round(float(get_prob(get_answer(res, "is_destructive"))), 2)
    git_prob = round(float(get_prob(get_answer(res, "modifies_git"))), 2)
    dest_conf = round(_noul_confidence(dest_prob), 4)
    git_conf = round(_noul_confidence(git_prob), 4)
    per_action = [
        _risk_action(a, cfg)
        for a in (dest_prob, git_prob)
    ]
    action = require_complete_context(worst_action(per_action), fitted["truncated"])
    confidence = round(min(dest_conf, git_conf), 4)
    safe = guardrail_safe(action, dest_prob, git_prob)

    reason_codes = []
    if dest_prob >= 0.20:
        reason_codes.append(f"destructive_prob={dest_prob:.2f}")
    if git_prob >= 0.20:
        reason_codes.append(f"git_modify_prob={git_prob:.2f}")
    if confidence < cfg.auto_accept:
        reason_codes.append(f"confidence={confidence:.2f}<{cfg.auto_accept:.2f}")
    if fitted["truncated"]:
        reason_codes.append("context_truncated")
    if action != "auto":
        reason_codes.append(f"action={action}")

    result = {
        "safe": safe,
        "destructive_prob": dest_prob,
        "git_modify_prob": git_prob,
        "action": action,
        "confidence": confidence,
        "reason_codes": reason_codes,
        "truncated": fitted["truncated"],
        "coverage": fitted["coverage"],
    }
    result.update(_response_meta(res, cfg))
    return result


# ----------------------------------------------------------------------
# 2. Agent Resource & Skill Finder (.agents & .opencode)
# ----------------------------------------------------------------------
def _answer_probs(res: Any, slot: str) -> Dict[str, float]:
    probs = _attr_opt(get_answer(res, slot), "probabilities")
    if not isinstance(probs, dict):
        return {}
    return {k: float(v) for k, v in probs.items()}


def find_agent_resources(task: str, root_dir: str = ".", max_matches: int = 5) -> dict:
    root = Path(root_dir).resolve()
    search_dirs = get_scan_paths(root)

    candidate_files: Dict[str, Path] = {}
    for sdir in search_dirs:
        if not sdir.exists():
            continue
        for p in sdir.rglob("*.md"):
            if p.is_file():
                try:
                    rel = p.relative_to(root).as_posix()
                except ValueError:
                    continue  # path is outside root, skip it
                candidate_files[rel] = p

    if not candidate_files:
        return {
            "matched": False,
            "count": 0,
            "resources": [],
            "summary": "No Markdown resources or skills found in candidate directories."
        }

    options = list(candidate_files.keys())[:MAX_CHOICE_OPTIONS]

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

    res, fitted, cfg = _request(
        f"User Task: {task}\nGoal: Identify which specific Markdown agent resources are directly relevant.",
        questions,
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
                content = f.read(MAX_CONTENT_CHARS)
        except Exception:
            content = ""

        name = full_path.parent.name if full_path.name.lower() == "skill.md" else full_path.stem
        resources.append({
            "name": name,
            "file": rel_path,
            "content": content
        })

    summary_names = ", ".join(r["name"] for r in resources)

    ranked_map: Dict[str, float] = {}
    for slot in ["primary", "secondary", "tertiary"]:
        for f, p in _answer_probs(res, slot).items():
            if f in candidate_files and f != "none":
                ranked_map[f] = max(ranked_map.get(f, 0.0), p)
    ranked = sorted(
        ({"file": f, "probability": round(p, 4)} for f, p in ranked_map.items()),
        key=lambda r: -r["probability"],
    )[:max_matches]

    slot_confs = [
        _slot_confidence(get_answer(res, slot))
        for slot in ["primary", "secondary", "tertiary"]
        if get_answer(res, slot) is not None
    ]
    confidence = round(min_confidence(slot_confs), 4)
    actions = [
        action_from_confidence(c, cfg.auto_accept, cfg.review_at)
        for c in slot_confs
    ]
    action = require_complete_context(
        worst_action(actions) if actions else "escalate",
        fitted["truncated"],
    )

    primary_prob = round(_answer_probs(res, "primary").get(primary_val, 0.0), 4) if primary_val else None

    result = {
        "matched": len(resources) > 0,
        "count": len(resources),
        "primary": resources[0] if resources else None,
        "file": resources[0]["file"] if resources else None,
        "content": resources[0]["content"] if resources else None,
        "resources": resources,
        "summary": f"Found {len(resources)} relevant agent resource(s): {summary_names}",
        "primary_probability": primary_prob,
        "ranked": ranked,
        "action": action,
        "confidence": confidence,
        "truncated": fitted["truncated"],
        "coverage": fitted["coverage"],
    }
    result.update(_response_meta(res, cfg))
    return result


# ----------------------------------------------------------------------
# 3. Fast Workspace File Selector
# ----------------------------------------------------------------------
def _exists_verdict(chosen, confidence: float) -> str:
    if chosen and chosen != "none":
        return "answered"
    return "absent" if confidence >= 0.35 else "partial"


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
        if len(candidates) >= MAX_CHOICE_OPTIONS:
            break

    if not candidates:
        return {"matched": False, "files": [], "exists": "absent"}

    criteria = {cand: "Candidate workspace file relevant to the task" for cand in candidates}
    criteria["none"] = "None of the supplied workspace files is relevant to the task"

    res, fitted, cfg = _request(
        f"User Task: {task}\nGoal: Identify which specific workspace files must be inspected or edited.",
        {"target_file": Choice(criteria=criteria, instructions="Select the primary workspace file that directly relates to this task.")},
    )

    target_ans = get_answer(res, "target_file")
    chosen = get_val(target_ans)
    probs = _answer_probs(res, "target_file")
    conf = _slot_confidence(target_ans)
    action = require_complete_context(
        action_from_confidence(conf, cfg.auto_accept, cfg.review_at),
        fitted["truncated"],
    )

    escaped = not chosen or chosen == "none"
    if not escaped:
        probability = round(probs.get(chosen, 0.0), 4)
        files = [chosen]
    else:
        probability = 0.0
        files = []

    ranked = sorted(
        ({"file": f, "probability": round(p, 4)} for f, p in probs.items() if f != "none"),
        key=lambda r: -r["probability"],
    )[:max_results]

    result = {
        "matched": not escaped,
        "files": files if not escaped else [],
        "exists": _exists_verdict(chosen, conf),
        "probability": probability,
        "ranked": ranked,
        "action": action,
        "confidence": round(conf, 4),
        "truncated": fitted["truncated"],
        "coverage": fitted["coverage"],
    }
    result.update(_response_meta(res, cfg))
    return result


# ----------------------------------------------------------------------
# 4. Model Tier Selection
# ----------------------------------------------------------------------
TIER_CRITERIA = {
    "fast": "Typos, simple lookups, docstrings, boilerplate",
    "balanced": "Standard bugs, test cases, isolated feature changes",
    "frontier": "Multi-file refactors, architecture design, complex algorithmic logic",
}


def select_model_tier(task: str) -> dict:
    """Select the optimal LLM model tier (fast, balanced, or frontier).

    Gated by `jevs_settings.enable_model_routing`. Low-confidence or truncated
    judgments report `action: "escalate"` while keeping `recommended_tier`.
    """
    settings = load_jev_settings()
    result = {
        "enabled": settings["enable_model_routing"],
        "task": task,
        "recommended_tier": None,
        "recommended_model": None,
        "model_map": settings["models"],
        "action": "review",
        "confidence": None,
        "truncated": False,
        "model": None,
        "usage": None,
    }
    if not settings["enable_model_routing"]:
        return result

    res, fitted, cfg = _request(
        f"User Task: {task}\nDetermine the appropriate model tier based on task complexity and reasoning required.",
        {"tier": Choice(criteria=TIER_CRITERIA, instructions="Select the appropriate model capability tier for this task.")},
    )

    tier_ans = get_answer(res, "tier")
    selected_tier = get_val(tier_ans) or "balanced"
    conf = _slot_confidence(tier_ans)
    action = require_complete_context(
        action_from_confidence(conf, cfg.auto_accept, cfg.review_at),
        fitted["truncated"],
    )

    result["recommended_tier"] = selected_tier
    result["recommended_model"] = settings["models"].get(selected_tier)
    result["action"] = action
    result["confidence"] = round(conf, 4)
    result["truncated"] = fitted["truncated"]
    result["coverage"] = fitted["coverage"]
    result["model"] = getattr(res, "model", None) or cfg.model or None
    result["usage"] = _usage_dict(_attr_opt(res, "usage"))
    return result


# ----------------------------------------------------------------------
# 5. CLI Entry Point
# ----------------------------------------------------------------------
def _cli_dispatch(argv) -> int:
    if len(argv) < 2:
        print(json.dumps({"error": {"code": "USAGE", "message": "Usage: python jev_engine.py [verify|resource|files] [args...]", "retryable": False}}))
        return 1
    action = argv[0].lower()
    try:
        if action == "verify":
            print(json.dumps(verify_command(argv[1])))
        elif action in ["resource", "find_resource"]:
            root_path = argv[2] if len(argv) > 2 else "."
            print(json.dumps(find_agent_resources(argv[1], root_path)))
        elif action in ["files", "target_files"]:
            root_path = argv[2] if len(argv) > 2 else "."
            print(json.dumps(select_target_files(argv[1], root_path)))
        elif action in ["tier", "model_tier"]:
            print(json.dumps(select_model_tier(argv[1])))
        else:
            print(json.dumps({"error": {"code": "USAGE", "message": f"Unknown action: {action}", "retryable": False}}))
            return 1
        return 0
    except Exception as e:
        print(json.dumps({"error": error_details(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(_cli_dispatch(sys.argv[1:]))