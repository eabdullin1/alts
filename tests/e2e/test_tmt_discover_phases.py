"""
Flow tests of a component task whose plan has several discover phases,
like the plans of tests/cups: tmt names a test /<phase>/<name> in the
results, but "tmt run tests --name", with which ALTS leaves the skipped
tests out of the run, sees /<name>. A skip rule must work in both forms,
or a test calling reboot runs and aborts the whole tmt run (pilot import
of 2026-11-26). Data: tests/fixtures/tmt/cups-al9.
"""

import re
from pathlib import Path

import pytest

from tests.e2e.test_tmt_task_flow import (  # noqa: F401 (fixture)
    FIXTURES,
    Flow,
    assert_init_container,
    flow,
    load_payload,
)

SCENARIO = FIXTURES / 'cups-al9'
PLAN = '/plans/tier2-tier3/public'
REBOOT_TEST = '/cups-public/Sanity/cups-systemd-units-smoke'
OTHER_TESTS = [
    '/cups-public/Regression/bz491190-sendff-doesnt-work',
    '/cups-extra/Regression/bz1659111-correct-permissions-on-logs',
]


def run_cups(flow: Flow, skipped_tests, dist_version: str = '9') -> dict:
    payload = load_payload('cups-al9')
    payload['skipped_tests'] = skipped_tests
    payload['dist_version'] = dist_version
    return flow.run(SCENARIO, payload)


@pytest.mark.parametrize(
    'dist_version, phase_key',
    [
        pytest.param('9', 'discover-phase', id='almalinux-9'),
        pytest.param('8', 'discover_phase', id='almalinux-8-tmt-1.26'),
    ],
)
@pytest.mark.parametrize(
    'skip_rule',
    [
        pytest.param(
            'tmt_test_/cups-public/Sanity/cups-systemd-units-smoke',
            id='with-phase-prefix',
        ),
        pytest.param(
            'tmt_test_/Sanity/cups-systemd-units-smoke',
            id='without-phase-prefix',
        ),
        pytest.param(
            'tmt_test_*/Sanity/cups-systemd-units-smoke',
            id='glob-for-both',
        ),
        pytest.param(
            'tmt_test_/cups-public/Sanity/cups-systemd-*',
            id='pattern-with-phase-prefix',
        ),
    ],
)
def test_skipped_reboot_test_is_not_run(
    flow: Flow,
    skip_rule: str,
    dist_version: str,
    phase_key: str,
):
    """
    The live check: the reboot test in skip, the rule in any form, is
    left out of the tmt run; the container keeps running, the other
    tests of both phases run and are reported, the reboot test is in
    skipped_tests.
    """
    result = run_cups(flow, [skip_rule], dist_version)

    (container,) = flow.containers().values()
    assert_init_container(
        container,
        f'almalinux/{dist_version}-init:{dist_version}',
    )
    assert container['running'] is True

    # ALTS has looked at the discovered tests: names with the phase prefix,
    # the phase in discover-phase (tmt 1.26: discover_phase)
    (discover,) = flow.tmt_runs(discover_only=True)
    assert discover['plan_regexes'] == [r'^/plans/tier2\-tier3/public$']
    tests_yaml = Path(
        flow.state, 'containers', next(iter(flow.containers())), 'fs',
        'var/tmp/tmt/discover', PLAN.lstrip('/'), 'discover', 'tests.yaml',
    ).read_text()
    assert f'{phase_key}: cups-public' in tests_yaml

    (tmt_run,) = flow.tmt_runs()
    (test_regex,) = tmt_run['test_regexes']
    # What tmt matches: the names without the phase prefix
    assert not re.search(test_regex, '/Sanity/cups-systemd-units-smoke')
    assert re.search(test_regex, '/Regression/bz491190-sendff-doesnt-work')
    assert re.search(
        test_regex, '/Regression/bz1659111-correct-permissions-on-logs',
    )
    assert tmt_run['executed'] == {PLAN: OTHER_TESTS}

    assert result['skipped_tests'] == [f'tmt_test_{REBOOT_TEST}']
    assert {
        key: (value['result'], value['success'], value['name'], value['plan'])
        for key, value in result['tmt'].items()
    } == {
        f'tmt_test_{name}': ('pass', True, name, PLAN) for name in OTHER_TESTS
    }
    tmt_tests = result['tmt_tests']
    assert tmt_tests['success'] is True
    assert tmt_tests['tmt_exit_code'] == 0
    assert tmt_tests['counts'] == {'pass': 2}
    assert tmt_tests['plans'] == [{'name': PLAN, 'counts': {'pass': 2}}]
    assert tmt_tests['excluded_plans'] == [
        {'name': '/plans/tier2-tier3/internal', 'reason': 'internal plan'},
    ]


@pytest.mark.parametrize(
    'skipped_tests',
    [
        pytest.param([], id='no-rule'),
        pytest.param(
            ['tmt_test_/cups-extra/Sanity/cups-systemd-units-smoke'],
            id='rule-of-another-phase',
        ),
    ],
)
def test_reboot_test_aborts_the_run(flow: Flow, skipped_tests):
    """
    Without a matching rule the reboot test runs: the container stops,
    the tests after it stay pending. Shows what the skip rule prevents.
    """
    result = run_cups(flow, skipped_tests)

    (container,) = flow.containers().values()
    assert container['running'] is False
    (tmt_run,) = flow.tmt_runs()
    assert REBOOT_TEST in tmt_run['executed'][PLAN]
    assert result['tmt'][f'tmt_test_{OTHER_TESTS[1]}']['result'] == 'pending'
    assert result['tmt_tests']['tmt_exit_code'] == 143
    assert result['skipped_tests'] == []
