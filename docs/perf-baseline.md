# Performance baseline

Machine: Intel(R) Core(TM) Ultra 7 255HX, Windows, Python 3.14 (.venv)
Measured: 2026-09-25  ·  Commit: ff606b2  ·  Phase: 1 (pre-optimization)

```
case                          median_ms   min_ms   max_ms   bytes
estimate_tokens_100k                2.2      2.2      2.3       -
estimate_tokens_750_options         1.2      1.1      1.2       -
fit_state_no_trunc                  0.0      0.0      0.1       -
fit_state_trunc                    13.9     13.6     14.3       -
mock_choice_250                     2.0      1.9      2.2       -
mock_system_one_250                 6.8      6.8      8.1       -
find_agent_resources_250           71.2     69.7     71.7   44198
find_agent_resources_250_warm       73.5     71.2     77.4   44198
select_target_files_git            15.7     14.7     16.6       -
envelope_size_skills                  -        -        -   44198
```

## Phase 5 targets

| Case | Phase 1 | Target | Budget (`--assert`) |
|---|---|---|---|
| estimate_tokens_100k | 2.2 ms | 4× faster (~0.5 ms) | 1.25× Phase 5 |
| mock_system_one_250 | 6.8 ms | 5× faster (~1.4 ms) | 1.25× Phase 5 |
| find_agent_resources_250 | 71.2 ms | 2× faster (~35.6 ms) | 1.25× Phase 5 |
| find_agent_resources_250_warm | 73.5 ms | 8× faster (cache) (~9.2 ms) | 1.25× Phase 5 |
| envelope_size_skills | 44198 B | ~50% smaller (~22 kB) | n/a |
