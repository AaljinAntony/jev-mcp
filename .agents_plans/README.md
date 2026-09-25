# Jev-TypeSafe MCP — Reliability Improvement Plans

Execute these plans **in order**. Each phase is self-contained and can be run
in a separate agent chat. Run `pytest tests/ -v` after each phase to verify
nothing broke.

| # | File | Focus | Priority | Effort |
|---|------|-------|----------|--------|
| 1 | [01_fix_error_ordering_and_add_retries.md](./01_fix_error_ordering_and_add_retries.md) | Fix `error_details()` isinstance ordering (BUG) + add SDK `RetryPolicy` | 🔴 Critical | ~1 hr |
| 2 | [02_fix_remaining_correctness_bugs.md](./02_fix_remaining_correctness_bugs.md) | Refactor `verify_command`, fix score validation key types, fix `relative_to` crash, remove dead code | 🟡 High | ~1.5 hr |
| 3 | [03_add_input_guardrails_security.md](./03_add_input_guardrails_security.md) | Input length limits + directory traversal prevention | 🟡 High | ~30 min |
| 4 | [04_client_caching_and_settings_fix.md](./04_client_caching_and_settings_fix.md) | Cache `TypeSafeClient` for connection reuse + CWD-independent settings | 🟡 High | ~1 hr |
| 5 | [05_performance_improvements.md](./05_performance_improvements.md) | Eliminate binary-search token estimation, use `git ls-files`, reduce serialization | 🟡 High | ~1.5 hr |
| 6 | [06_expand_test_coverage.md](./06_expand_test_coverage.md) | New tests: error mapping, path traversal, logging, edge cases | 🟡 High | ~1 hr |
| 7 | [07_plugin_improvements.md](./07_plugin_improvements.md) | Fix hardcoded paths, quote stripping, content limits, diagnostic logging | 🟡 High | ~1 hr |
| 8 | [08_low_priority_cleanup.md](./08_low_priority_cleanup.md) | Symlink protection, improved log redaction, dotenv deduplication | 🟢 Low | ~30 min |

**Total estimated effort: ~8 hours**

## How to use with an agent

Copy-paste or reference each plan file as the task prompt. Each plan contains:
- **Problem description** with exact file paths and line numbers
- **Exact code changes** (diff-style or replacement blocks)
- **Design decisions** explaining why the fix was chosen
- **Validation steps** (which tests to run)
- **Checklist** of all changes needed

## Dependencies between phases

- Phase 2 depends on Phase 1 (Phase 1 adds `RetryPolicy` to the import, Phase 2's `verify_command` refactor benefits from it)
- Phase 6 (tests) references fixes from Phases 1–3 — mark tests as `@pytest.mark.skip` if the corresponding fix isn't done yet
- All other phases are independent and can be reordered if needed
