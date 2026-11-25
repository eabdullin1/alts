# A real ALBS -> ALTS -> ALBS run

Data of `tests/e2e/test_tmt_live_capture.py`, captured on 24 November 2026 on
the albs-test stand: ALBS `package-tests-tmt` and ALTS `tmt-tests-stage`, a
build of grep (`imports/c9/grep-3.6-5.el9`) for AlmaLinux 9 x86_64. A logging
proxy stood between ALTS and ALBS, the tmt run directory was copied out of the
worker before ALTS removed it, the stage log was taken from Pulp. Only the
internal host names of the stand were replaced (`pulp.example.org`,
`git.example.org`, `albs.example.org`); nothing else was edited.

| File | What it is |
|---|---|
| `albs_payloads.json` | The `GET /api/v1/tests/get_test_tasks/` answer of ALBS as it came: the package test tasks (`package_tests: null`) and the component one |
| `task_id` | The ALTS task ID of the component task (log names carry it) |
| `results/<plan>/execute/results.yaml` | `results.yaml` of every plan as tmt 1.78 wrote it, with the `output.txt` of every test |
| `scenario.yaml` | What the fake tools need besides: the commit, the plans, their `tmt plans export` (from `plans/*.fmf` of the repository) |
| `tmt_tests_stage.txt` | The part of the `tmt_tests` stage log ALTS uploaded that ALTS writes itself: the context, the skipped tests, the tmt command it ran (it predates `-e HOME=/root`, the tests add it), the exit code and the counts. The output of tmt that follows it in the log isn't kept. `.txt`, not `.log`: `*.log` is in `.gitignore` |
| `alts_result.json` | The `POST /api/v1/tests/<id>/result/` body ALTS sent to ALBS |

## grep-al9

`tests/grep` of git.almalinux.org at `1531e51` (head of `main`), its three
public plans. tier1: 6 pass; tier2-tier3: 19 pass, 2 fail; others: no tests.

## e2e-plans

The same build retested with a copy of `tests/grep` (`6d5e624`) with three
tests of its own: `/e2e/pass` and `/e2e/skipme` in `/plans/e2e/gating`,
`/e2e/fail` in `/plans/e2e/info`, and `skipped_tests: [tmt_test_/e2e/skipme]`
in the payload. tmt didn't run `/e2e/skipme`, so the real `results.yaml` of
the gating plan doesn't have it; `extra_tests` of `scenario.yaml` gives it to
the fake tmt (failing), which is the only thing here not from the stand.
