import re
from typing import List, Optional

import pytest

from alts.worker.tmt import (
    DiscoveredTest,
    TmtResult,
    build_distro_context,
    build_test_name_regex,
    build_tmt_command,
    build_tmt_context,
    count_outcomes,
    extract_step_errors,
    find_internal_plans,
    get_excluded_tests,
    is_tmt_test_skipped,
    make_result_keys,
    parse_discovered_tests,
    parse_os_release,
    parse_results,
    parse_step_status,
    read_output_tail,
)

TMT_PATH = 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
# tmt runs the tests without HOME (foomatic-rip of cups-filters crashes)
TMT_HOME = 'HOME=/root'


def tmt_selects(regex: Optional[str], name: str) -> bool:
    """tmt selects the test if "tests --name" regex is found in its name."""
    return regex is None or re.search(regex, name) is not None


@pytest.mark.parametrize(
    'skipped_tests, selected, excluded',
    [
        pytest.param(
            None,
            ['/Sanity/basic'],
            [],
            id='nothing-skipped',
        ),
        pytest.param(
            ['third_party_test_smoke.sh', 'uninstall_package', 'tmt_tests'],
            ['/Sanity/basic'],
            [],
            id='no-tmt-test-rules',
        ),
        pytest.param(
            ['tmt_test_/Sanity/cups-systemd-units-smoke'],
            [
                '/Sanity/basic',
                '/Sanity/cups-systemd-units-smoke-2',
                '/Regression/Sanity/cups-systemd-units-smoke',
            ],
            ['/Sanity/cups-systemd-units-smoke'],
            id='exact-name',
        ),
        pytest.param(
            ['tmt_test_/Regression/RHEL-9973-*', 'tmt_test_/Sanity/a?c'],
            ['/Regression/RHEL-997', '/Sanity/ac', '/Sanity/abcd'],
            ['/Regression/RHEL-9973-x', '/Regression/RHEL-9973-', '/Sanity/abc'],
            id='patterns',
        ),
        pytest.param(
            ['tmt_test_/Sanity/bz[0-9]*', 'tmt_test_/x/[!a]'],
            ['/Sanity/bza', '/x/a'],
            ['/Sanity/bz1', '/x/b'],
            id='character-classes',
        ),
        pytest.param(
            ['tmt_test_/a.b+c(d)'],
            ['/aXb+c(d)', '/a.bbc(d)'],
            ['/a.b+c(d)'],
            id='regex-characters-are-literal',
        ),
        pytest.param(
            ['tmt_test_*'],
            [],
            ['/Sanity/basic', '/a'],
            id='everything',
        ),
    ],
)
def test_build_test_name_regex(
    skipped_tests: Optional[List[str]],
    selected: List[str],
    excluded: List[str],
):
    regex = build_test_name_regex(skipped_tests or [])
    if not any(
        test.startswith('tmt_test_') for test in skipped_tests or []
    ):
        assert regex is None
    for name in selected:
        assert tmt_selects(regex, name), name
        assert not is_tmt_test_skipped(name, skipped_tests or [])
    for name in excluded:
        assert not tmt_selects(regex, name), name
        assert is_tmt_test_skipped(name, skipped_tests or [])


@pytest.mark.parametrize(
    'os_release, dist_version, expected',
    [
        pytest.param(
            'NAME="AlmaLinux"\nVERSION="9.7 (Moss Jungle Cat)"\n'
            'ID="almalinux"\nVERSION_ID="9.7"\n',
            '9',
            'almalinux-9.7,centos-stream-9,rhel-9.7',
            id='almalinux-9',
        ),
        pytest.param(
            'NAME="AlmaLinux"\nVERSION_ID="8.10"\n',
            '8',
            'almalinux-8.10,centos-stream-8,rhel-8.10',
            id='almalinux-8',
        ),
        pytest.param(
            'NAME="AlmaLinux Kitten"\nVERSION="10 (Lion Cub)"\n'
            'VERSION_ID="10"\n',
            '10-kitten',
            'almalinux-kitten-10,centos-stream-10',
            id='kitten',
        ),
        pytest.param(
            '',
            '10.1',
            'almalinux-10.1,centos-stream-10,rhel-10.1',
            id='no-os-release',
        ),
    ],
)
def test_build_distro_context(os_release: str, dist_version: str, expected):
    assert build_distro_context(
        parse_os_release(os_release),
        dist_version,
    ) == expected


def test_build_tmt_context():
    context, ignored = build_tmt_context(
        {'VERSION_ID': '9.7', 'NAME': 'AlmaLinux'},
        '9',
        'x86_64_v2',
        {
            'component': 'chrony',
            'distro': 'fedora-42',
            'trigger': ['build', 'commit'],
        },
    )
    assert context == {
        'distro': 'almalinux-9.7,centos-stream-9,rhel-9.7',
        'arch': 'x86_64',
        'initiator': 'albs',
        'component': 'chrony',
        'trigger': 'build,commit',
    }
    assert ignored == ['distro']


def test_build_tmt_command():
    cmd = build_tmt_command(
        ['/plans/tier1/public', '/plans/others/public'],
        {'distro': 'almalinux-9.7,centos-stream-9,rhel-9.7', 'arch': 'x86_64'},
        environment={
            'TEST_PROFILE': 'container', 'PATH': '/bin', 'HOME': '/home/x',
        },
        test_filter='tier: 1',
        test_name_regex='^(?!(?:/Sanity/x)$)',
        feeling_safe=True,
        timeout=5400,
    )
    assert cmd == [
        'timeout', '--kill-after=60', '5400',
        'tmt', '--feeling-safe',
        '-c', 'distro=almalinux-9.7,centos-stream-9,rhel-9.7',
        '-c', 'arch=x86_64',
        'run', '--id', '/var/tmp/tmt/run',
        '-e', 'TEST_PROFILE=container',
        '-e', TMT_HOME,
        '-e', TMT_PATH,
        '--all', 'provision', '--how', 'local',
        'plans',
        '--name', r'^/plans/tier1/public$',
        '--name', r'^/plans/others/public$',
        'tests', '--filter', 'tier: 1', '--name', '^(?!(?:/Sanity/x)$)',
    ]


def test_build_tmt_command_el8():
    # tmt 1.26 from EPEL 8 has no --feeling-safe
    cmd = build_tmt_command(
        ['/plans/tier1/public'],
        {'distro': 'almalinux-8.10,centos-stream-8,rhel-8.10'},
        feeling_safe=False,
    )
    assert cmd == [
        'tmt',
        '-c', 'distro=almalinux-8.10,centos-stream-8,rhel-8.10',
        'run', '--id', '/var/tmp/tmt/run',
        '-e', TMT_HOME,
        '-e', TMT_PATH,
        '--all', 'provision', '--how', 'local',
        'plans', '--name', r'^/plans/tier1/public$',
    ]
    for plan in ('/plans/tier1/public',):
        assert re.search(cmd[-1], plan)
    assert not re.search(cmd[-1], '/plans/tier1/public-extra')
    assert not re.search(cmd[-1], '/plans/tier1/internal')


def test_find_internal_plans():
    exported = [
        {
            'name': '/plans/public',
            'discover': {'how': 'fmf', 'filter': 'tier: 1'},
        },
        {
            'name': '/plans/gitlab',
            'discover': [
                {'how': 'fmf'},
                {'how': 'fmf', 'url': 'https://gitlab.com/redhat/rhel/tests/x'},
            ],
        },
        {
            'name': '/plans/import',
            'plan': {'import': {'url': 'https://pkgs.devel.redhat.com/x'}},
        },
    ]
    assert find_internal_plans(
        [
            '/plans/public',
            '/plans/tier1/internal',
            '/plans/gitlab',
            '/plans/import',
            '/plans/not-exported',
        ],
        exported,
    ) == {
        '/plans/tier1/internal': 'internal plan',
        '/plans/gitlab': (
            'uses internal Red Hat repository '
            'https://gitlab.com/redhat/rhel/tests/x'
        ),
        '/plans/import': (
            'uses internal Red Hat repository https://pkgs.devel.redhat.com/x'
        ),
    }


RESULTS_YAML = """
- name: /Sanity/basic
  serial-number: 1
  result: pass
  note: []
  duration: '00:00:12'
  log:
    - data/guest/default-0/Sanity/basic-1/output.txt
    - data/guest/default-0/Sanity/basic-1/journal.txt
- name: /Sanity/warned
  result: warn
  note:
    - beakerlib WARN
  log: []
- name: /Regression/failed
  result: fail
  note: 'check failed'
  duration: '00:01:00'
- name: /Regression/skipped
  result: skip
- result: pass
"""


def test_parse_results():
    results = parse_results(RESULTS_YAML, '/plans/tier1/public')
    assert results == [
        TmtResult(
            name='/Sanity/basic',
            plan='/plans/tier1/public',
            result='pass',
            note='',
            duration='00:00:12',
            logs=[
                'data/guest/default-0/Sanity/basic-1/output.txt',
                'data/guest/default-0/Sanity/basic-1/journal.txt',
            ],
        ),
        TmtResult(
            name='/Sanity/warned',
            plan='/plans/tier1/public',
            result='warn',
            note='beakerlib WARN',
        ),
        TmtResult(
            name='/Regression/failed',
            plan='/plans/tier1/public',
            result='fail',
            note='check failed',
            duration='00:01:00',
        ),
        TmtResult(
            name='/Regression/skipped',
            plan='/plans/tier1/public',
            result='skip',
        ),
    ]
    # D-20: warn doesn't fail the task
    assert [result.success for result in results] == [
        True, True, False, False,
    ]
    assert [result.skipped for result in results] == [
        False, False, False, True,
    ]
    assert count_outcomes(results) == {
        'pass': 1, 'warn': 1, 'fail': 1, 'skip': 1,
    }


@pytest.mark.parametrize('text', ['', '[]', 'null'])
def test_parse_empty_results(text: str):
    assert parse_results(text, '/plans/x') == []


def test_parse_discovered_tests():
    assert parse_discovered_tests(
        '- name: /Sanity/a\n  path: /Sanity/a\n- name: /b\n- summary: x\n'
    ) == [DiscoveredTest('/Sanity/a'), DiscoveredTest('/b')]


@pytest.mark.parametrize('phase_key', ['discover-phase', 'discover_phase'])
def test_parse_discovered_tests_of_several_phases(phase_key: str):
    # discover-phase: tmt 1.78, discover_phase: tmt 1.26 (EPEL 8)
    tests = parse_discovered_tests(
        f'- name: /cups-public/Sanity/smoke\n  {phase_key}: cups-public\n'
        f'- name: /cups-filters-public/Sanity/x\n'
        f'  {phase_key}: cups-filters-public\n'
    )
    assert tests == [
        DiscoveredTest('/cups-public/Sanity/smoke', 'cups-public'),
        DiscoveredTest('/cups-filters-public/Sanity/x', 'cups-filters-public'),
    ]
    assert [test.filter_name for test in tests] == [
        '/Sanity/smoke',
        '/Sanity/x',
    ]


@pytest.mark.parametrize(
    'test, filter_name',
    [
        pytest.param(DiscoveredTest('/Sanity/a'), '/Sanity/a', id='no-phase'),
        # A single discover phase: tmt doesn't add the prefix
        pytest.param(
            DiscoveredTest('/Sanity/a', 'default-0'),
            '/Sanity/a',
            id='single-phase',
        ),
        pytest.param(
            DiscoveredTest('/cups-public/Sanity/a', 'cups-public'),
            '/Sanity/a',
            id='phase-prefix',
        ),
        pytest.param(
            DiscoveredTest('/cups-publicity/a', 'cups-public'),
            '/cups-publicity/a',
            id='not-a-whole-segment',
        ),
    ],
)
def test_discovered_test_filter_name(test: DiscoveredTest, filter_name: str):
    assert test.filter_name == filter_name


# cups: the tier1 plan has two discover phases
CUPS_DISCOVERED = [
    DiscoveredTest('/cups-public/Sanity/basic-commands', 'cups-public'),
    DiscoveredTest(
        '/cups-public/Sanity/cups-systemd-units-smoke',
        'cups-public',
    ),
    DiscoveredTest(
        '/cups-public/Sanity/bz1257751-Email-notifications-lp-m-for-'
        'printer-classes',
        'cups-public',
    ),
    DiscoveredTest('/cups-filters-public/Sanity/smoke', 'cups-filters-public'),
    DiscoveredTest('/cups-filters-public/Sanity/other', 'cups-filters-public'),
]


@pytest.mark.parametrize(
    'skipped_tests, excluded',
    [
        pytest.param(
            ['tmt_test_/cups-public/Sanity/cups-systemd-units-smoke'],
            ['/cups-public/Sanity/cups-systemd-units-smoke'],
            id='with-phase-prefix',
        ),
        pytest.param(
            ['tmt_test_/Sanity/cups-systemd-units-smoke'],
            ['/cups-public/Sanity/cups-systemd-units-smoke'],
            id='without-phase-prefix',
        ),
        pytest.param(
            ['tmt_test_*/Sanity/cups-systemd-units-smoke'],
            ['/cups-public/Sanity/cups-systemd-units-smoke'],
            id='glob-for-both',
        ),
        pytest.param(
            ['tmt_test_/cups-public/Sanity/bz1257751-*'],
            [
                '/cups-public/Sanity/bz1257751-Email-notifications-lp-m-for-'
                'printer-classes',
            ],
            id='pattern-with-phase-prefix',
        ),
        pytest.param(
            ['tmt_test_/cups-filters-public/Sanity/*'],
            [
                '/cups-filters-public/Sanity/smoke',
                '/cups-filters-public/Sanity/other',
            ],
            id='whole-phase',
        ),
        pytest.param(
            ['tmt_test_/cups-public/Sanity/smoke'],
            [],
            id='other-phase',
        ),
    ],
)
def test_skip_rules_with_phase_prefix(
    skipped_tests: List[str],
    excluded: List[str],
):
    """
    A skip rule matches a test of a plan with several discover phases by
    the name of the results (/<phase>/<name>) and by the name without
    the prefix, which "tmt run tests --name" matches: the test is left
    out of the run in both cases.
    """
    regex = build_test_name_regex(skipped_tests, CUPS_DISCOVERED)
    assert [
        test.name for test in get_excluded_tests(CUPS_DISCOVERED, regex)
    ] == excluded
    for test in CUPS_DISCOVERED:
        # What tmt itself does with the regex
        assert tmt_selects(regex, test.filter_name) == (
            test.name not in excluded
        ), test.name
        assert is_tmt_test_skipped(
            test.name,
            skipped_tests,
            test.filter_name,
        ) == (test.name in excluded), test.name


def test_skip_rule_with_phase_prefix_without_discovery():
    # Without the discovered tests only the results can be matched
    skipped_tests = ['tmt_test_/cups-public/Sanity/cups-systemd-units-smoke']
    regex = build_test_name_regex(skipped_tests)
    assert tmt_selects(regex, '/Sanity/cups-systemd-units-smoke')
    assert is_tmt_test_skipped(
        '/cups-public/Sanity/cups-systemd-units-smoke',
        skipped_tests,
    )


def test_get_excluded_tests_without_regex():
    assert get_excluded_tests(CUPS_DISCOVERED, None) == []


def test_make_result_keys():
    results = [
        TmtResult('/a', '/plans/tier1/public', 'pass'),
        TmtResult('/b', '/plans/tier1/public', 'pass'),
        TmtResult('/a', '/plans/all/public', 'fail'),
    ]
    assert make_result_keys(results) == [
        'tmt_test_/a@/plans/tier1/public',
        'tmt_test_/b',
        'tmt_test_/a@/plans/all/public',
    ]


def test_read_output_tail():
    text = '\n'.join(f'line {i}' for i in range(100)) + '\n'
    assert read_output_tail(text, lines=2) == 'line 98\nline 99'
    assert read_output_tail('x' * 50, limit=10) == 'x' * 10


@pytest.mark.parametrize(
    'text, status',
    [
        pytest.param('status: done\ndata: []\n', 'done', id='done'),
        pytest.param('status: todo\ndata:\n- how: fmf\n', 'todo', id='todo'),
        pytest.param('', None, id='empty'),
        pytest.param('- x\n', None, id='not-a-mapping'),
    ],
)
def test_parse_step_status(text: str, status: Optional[str]):
    assert parse_step_status(text) == status


# tmt 1.78 output of a plan whose discover phase can't clone its
# repository (live run of tests/cups, 2026-11-26)
DISCOVER_FAILED_OUTPUT = """
/var/tmp/tmt/run
warn: User is feeling safe: all unsafe behavior allowed.

/plans/tier2-tier3/public
    discover
        directory: /var/tmp/tmt-tests/cups
        how: fmf
        name: cups-public
        filter: tier: 3
        url: https://git.almalinux.org/tests/cups-filters.git
        fail: Command 'git clone https://git.almalinux.org/tests/cups-filters.git /var/tmp/tmt/run/plans/tier2-tier3/public/discover/cups-filters-public/tests' returned 128.
        fail: Command 'git clone https://git.almalinux.org/tests/cups-filters.git /var/tmp/tmt/run/plans/tier2-tier3/public/discover/cups-filters-public/tests' returned 128.
    report
        how: display
        summary: no results found
    cleanup
        warn: Nothing to cleanup, no guests provisioned.

/plans/tier1/public
    discover
        fail: something else
    execute
        fail: not discover

plan failed.
"""


def test_extract_step_errors():
    assert extract_step_errors(
        DISCOVER_FAILED_OUTPUT, '/plans/tier2-tier3/public',
    ) == [
        "fail: Command 'git clone https://git.almalinux.org/tests/"
        "cups-filters.git /var/tmp/tmt/run/plans/tier2-tier3/public/"
        "discover/cups-filters-public/tests' returned 128.",
    ]
    assert extract_step_errors(
        DISCOVER_FAILED_OUTPUT, '/plans/tier1/public',
    ) == [
        'fail: something else',
    ]
    assert extract_step_errors(DISCOVER_FAILED_OUTPUT, '/plans/other') == []
    assert extract_step_errors('', '/plans/tier1/public') == []


REPO = 'https://git.almalinux.org/tests/alts-live-check-missing-repository.git'
CLONE = (
    f"Command 'git clone {REPO} "
    "/var/tmp/tmt/run/plans/broken/discover/missing-public/tests' "
    "returned 128."
)
FATAL = f"fatal: Authentication failed for '{REPO}/'"
GIT_STDERR = (
    "        Cloning into '/var/tmp/tmt/run/plans/broken/discover/"
    "missing-public/tests'...\n"
    '        remote: Credentials are incorrect or have expired.\n'
    f'        {FATAL}\n'
)

# The live runs of a plan with a discover phase whose repository can't
# be cloned (tests/grep, 2026-11-26), shortened
TMT_1_78_DISCOVER_FAILED = f"""
/var/tmp/tmt/run
warn: User is feeling safe: all unsafe behavior allowed.

/plans/broken
    discover
        how: fmf
        name: grep-public
        filter: tier: 1
        url: {REPO}
        fail: {CLONE}
        fail: {CLONE}
    report
        how: display
        summary: no results found
    cleanup
        warn: Nothing to cleanup, no guests provisioned.

plan failed.

The exception was caused by 1 earlier exceptions

Cause number 1:

    Retries of 'git clone {REPO}' unsuccessful.

    The exception was caused by 3 earlier exceptions

    Cause number 1:

        {CLONE}

        # stderr (3/3 lines)
        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
{GIT_STDERR}        # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    Cause number 2:

        {CLONE}
"""

TMT_1_26_DISCOVER_FAILED = f"""
/var/tmp/tmt/run

/plans/broken
    discover
        warn: /Regression/x: - 'author', 'check' do not match any of the \
regexes: '^extra-'
        how: fmf
        name: missing-public
        url: {REPO}
    finish
\x20\x20\x20\x20
        summary: 0 tasks completed

\x1b[31mplan failed\x1b[0m

The exception was caused by 1 earlier exceptions

Cause number 1:

    \x1b[31m{CLONE}\x1b[0m
    stderr (3/3 lines)
    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
{GIT_STDERR}    ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
"""


@pytest.mark.parametrize(
    'output, errors',
    [
        pytest.param(
            TMT_1_78_DISCOVER_FAILED,
            [
                f'fail: {CLONE}',
                f"Retries of 'git clone {REPO}' unsuccessful.",
                FATAL,
            ],
            id='tmt-1.78',
        ),
        pytest.param(
            TMT_1_26_DISCOVER_FAILED, [CLONE, FATAL], id='tmt-1.26',
        ),
    ],
)
def test_extract_step_errors_live(output: str, errors: List[str]):
    """The causes are taken, warnings and ANSI colors are not."""
    assert extract_step_errors(output, '/plans/broken') == errors
