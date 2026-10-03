import sys
import tempfile
from pathlib import Path

import pytest
from typesafe_sdk import ChoiceAnswer, SystemOneResponse, Usage

# Make the repo root importable from tests/ (repo modules are not a package).
ROOT = str(Path(__file__).resolve().parent.parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def stub_choice(monkeypatch):
    """Answer a Choice question with a hand-built distribution, no SDK call.

    Patches `jev_engine._request` — the single seam every tool funnels through —
    so the test controls the judgment the policy layer will see while the
    discovery, validation and decision code around it still runs for real.

    Lives here rather than in each test module because several modules need the
    same stub, and because a stub that drifts from the SDK's answer shape breaks
    validation rather than the test. A test that needs to *inspect* the questions
    (criteria keys, state) still writes its own `_request`, since the fixture
    discards them.

    Usage::

        stub_choice(".agents/skills/a/SKILL.md",
                    {".agents/skills/a/SKILL.md": 0.8, "none": 0.2})

    With a second answer — `search_target_files` asks a Choice and a Noul::

        stub_choice("none", {"a.py": 0.1, "none": 0.9},
                    key="target_file", confidence=0.7,
                    extra={"is_relevant": NoulAnswer(noul=0.1)})
    """
    import jev_engine

    def _install(choice, probabilities, *, key="primary", confidence=0.9, extra=None):
        answers = {
            key: ChoiceAnswer(
                choice=choice,
                probabilities=dict(probabilities),
                confidence=confidence,
            )
        }
        if extra:
            answers.update(extra)

        def _fake_request(state, questions):
            return (
                SystemOneResponse(
                    model="jev-test",
                    answers=answers,
                    usage=Usage(input_tokens=12, output_tokens=6),
                ),
                {"truncated": False, "coverage": {}},
                jev_engine.get_config(),
            )

        monkeypatch.setattr(jev_engine, "_request", _fake_request)
        return _fake_request

    return _install


@pytest.fixture(autouse=True)
def _grant_tempdir_as_allowed_root(monkeypatch):
    """Let `root_dir` point inside the system temp dir.

    `root_dir` is confined to an allowlist (CWD + ancestors +
    `JEV_MCP_ALLOWED_ROOTS`). pytest's `tmp_path` lives under the system temp
    dir, which is deliberately *not* on that list — a test asking about
    `tmp_path` is a legitimate request, not an escape. Granting the temp dir
    here keeps those tests meaningful instead of weakening the allowlist.
    """
    monkeypatch.setenv("JEV_MCP_ALLOWED_ROOTS", tempfile.gettempdir())


@pytest.fixture(autouse=True)
def _no_machine_global_skills(monkeypatch):
    """Keep the suite independent of the skills installed on the machine.

    `find_agent_resources` now scans the current user's global skill
    directories (see `jev_engine._global_scan_dirs`). Without this, a
    developer's real `~/.agents/skills` enters every candidate count and every
    offline-judge ranking, and the suite's verdicts depend on which machine ran
    it. Tests that exercise the global scan restore the real function
    themselves — see `tests/test_global_scan.py`.
    """
    import jev_engine

    monkeypatch.setattr(jev_engine, "_global_scan_dirs", lambda: [])


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Clear the module-level config/client/settings/breaker/scan caches around every test.

    `config.get_config`, `jev_engine.get_client`, `jev_engine.load_jev_settings`,
    the circuit breaker and the workspace scan cache all memoize process-wide
    state. Without this, a test that changes the environment (e.g.
    `JEV_MCP_MOCK`) is silently ignored by whichever test ran after it and
    cached the previous environment — and a test that creates files under
    `tmp_path` would be served another test's stale scan results.
    """
    import config
    import jev_engine

    def _clear():
        config._reset_config_cache()
        jev_engine._reset_client_cache()
        jev_engine._reset_settings_cache()
        jev_engine._reset_breaker()
        jev_engine._reset_scan_cache()

    _clear()
    yield
    _clear()
