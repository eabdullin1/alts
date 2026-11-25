# tmt task fixtures

Data of the flow tests `tests/e2e/test_tmt_task_flow.py`: an ALTS task from
the payload ALBS returns from `get_test_tasks` to the result ALTS posts back.
They repeat the live check ALBS → ALTS → ALBS on the albs-test stand
(24 November 2026). Host names, repository URLs with credentials and tokens
are replaced with placeholders. The same files describe the format for the
ALBS side.

Each scenario directory has:

| File | What it is |
|---|---|
| `payload.json` | One item of the ALBS `get_test_tasks` response, `TaskRequestPayload` with `package_tests` |
| `scenario.yaml` | What the fake tools (`tests/e2e/fake_tools.py`) know: the commit `tests/<pkg>` has, the plans of the repository, the `tmt plans export` output; `reboot: <test>` stops the container after that test; `phases: {<plan>: [<phase>, ...]}` gives a plan several discover phases: its tests are named `/<phase>/<name>` in discover and in the results, and the fake tmt matches `tests --name` against `/<name>`, as tmt does; `discover_fail: {<plan>: <error>}` makes the discover step of a plan fail: tmt leaves its `discover/step.yaml` in `todo`, writes an empty `results.yaml`, prints `fail: <error>` under the discover step of the plan and exits with 2; later plans don't run, as with tmt 1.78 |
| `results/<plan>/execute/results.yaml` | Results of a plan as tmt 1.78 writes them. The fake tmt takes the tests of the plan from here, drops the tests its `--name` regexes exclude, and writes them into the container as tmt does |
| `expected_result.json` | The `result` ALTS posts to ALBS, without `logs` |

## grep-al9

A build of grep, AlmaLinux 9 x86_64, `tests/grep` at
`1531e510fe5f35bef344c637fefced85038c69f7` (head of `main`). The payload has the
three public plans of the repository, `context` with `trigger` and
`component`. Every plan of the request is required, the request has no
gating flag; if one comes, ALTS ignores it.
Live result: tier1 6 pass; tier2-tier3 19 pass and 2 fail
(`/Regression/inconsistent-w-and-alnum-behaviour`,
`/Regression/bz1050916-malformed-output-when-coloring-is-on`); others no tests;
`tmt_tests.success` true with `tmt_exit_code` 1.

The names are the tests of `tests/grep` by tier. Tiers 2 and 3 have 22 tests,
the live run reported 21; which one tmt left out isn't recorded, the fixture
omits `/Sanity/rpmbuild-rebuild-with-check`. Durations are made up.

`ALTS_UPDATE_EXPECTED=1 pytest tests/e2e` rewrites `expected_result.json`
after an intended change of the result format.

## e2e-plans

The synthetic plans of the live check, `skipped_tests: ["tmt_test_/e2e/skipme"]`:

- `/plans/e2e/gating`: `/e2e/pass`, `/e2e/fail`, `/e2e/skipme` (it would fail
  if it ran);
- `/plans/e2e/info`: `/e2e/pass` again (two plans → keys
  `tmt_test_/e2e/pass@<plan>`), `/e2e/warn`, `/e2e/skipped-by-tmt` (tmt outcome
  skip → `skipped_tests`);
- `/plans/e2e/internal`: discovers from an internal Red Hat repository, ALTS
  doesn't run it (`excluded_plans`). Not in the live check, added here.

## cups-al9

`tests/cups` with several discover phases in a plan (live check on the test
host, 26 November 2026, AlmaLinux 9 x86_64, cups-2.3.3op2-39.el9_8). The real
`/plans/tier2-tier3/public` has the phases `cups-public` and
`cups-filters-public`, the second one clones `tests/cups-filters`, which
git.almalinux.org doesn't serve yet; the live check used a copy of the plan
whose second phase, `cups-extra`, takes one test of `tests/cups` itself
(commit `e5cc26c`). tmt named the tests `/cups-public/...` and
`/cups-extra/...` in discover (`discover-phase` of `tests.yaml`) and in the
results.

`skipped_tests: ["tmt_test_/cups-public/Sanity/cups-systemd-units-smoke"]`,
the name as the results give it, with the phase prefix: the test calls
reboot. ALTS left it out of the run with
`tests --name '^(?!(?:/cups\-public/Sanity/cups\-systemd\-units\-smoke|/Sanity/cups\-systemd\-units\-smoke)$)'`;
tmt ran the other two tests, both pass, and reported the reboot test in
`skipped_tests`. `results.yaml` adds the reboot test the live run didn't
have (`reboot:` of `scenario.yaml`: if it runs, the container stops).
