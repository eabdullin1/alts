"""
Helpers of the tmt tests stage: tmt command line, test exclusion,
tmt context and parsing of the tmt results.

The stage itself is BaseRunner.run_tmt_tests().
"""

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

__all__ = [
    'TMT_SECTION_NAME',
    'TMT_STAGE_NAME',
    'TMT_TEST_PREFIX',
    'DiscoveredTest',
    'TmtResult',
    'build_tmt_command',
    'build_tmt_context',
    'build_distro_context',
    'build_test_name_regex',
    'extract_step_errors',
    'find_internal_plans',
    'get_excluded_tests',
    'get_tmt_test_key',
    'glob_to_regex',
    'is_tmt_test_skipped',
    'parse_discovered_tests',
    'parse_os_release',
    'parse_results',
    'parse_step_status',
    'read_output_tail',
]

# Result section of the single tmt tests, like 'third_party' for the
# third party tests
TMT_SECTION_NAME = 'tmt'
# The whole stage, a TESTS_MAPPING value
TMT_STAGE_NAME = 'tmt_tests'
# A single tmt test in the result and in skipped_tests (D-9)
TMT_TEST_PREFIX = 'tmt_test_'

# In "provision --how local" tmt gives the tests a PATH without /usr/sbin
TMT_PATH = '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
# ... and no HOME: foomatic-rip of the cups-filters tests crashes without
# it (exit code 139 on AlmaLinux 10 and Kitten)
TMT_HOME = '/root'
# Set by ALTS for every test, "-e KEY=VALUE" of "tmt run" (tmt 1.26 and
# newer); the environment of the payload can't override them
TMT_ENVIRONMENT = {'HOME': TMT_HOME, 'PATH': TMT_PATH}
# tmt work directory like in CentOS CI: the test user can't read
# the test files in the home directory of root
TMT_RUN_DIR = '/var/tmp/tmt/run'
TMT_DISCOVER_DIR = '/var/tmp/tmt/discover'
TMT_TESTS_DIR = '/var/tmp/tmt-tests'
# Where CentOS CI puts the RPMs of the build, tests look for them there
TEST_ARTIFACTS_DIR = '/var/share/test-artifacts'

# D-20: warn doesn't fail the task, it's shown separately
SUCCESS_OUTCOMES = frozenset(('pass', 'info', 'warn'))
SKIP_OUTCOMES = frozenset(('skip',))
# Everything else (fail, error, pending: the run was aborted before
# the test finished) is a failure

# Plans that need Red Hat infrastructure (ALTS-14)
INTERNAL_PLAN_MARKERS = (
    'pkgs.devel.redhat.com',
    'gitlab.cee.redhat.com',
    'gitlab.com/redhat/rhel',
)
INTERNAL_PLAN_NAME_RE = re.compile(r'(^|/)internal(/|$)')

# Context keys ALTS sets itself, ALBS can't override them
ALTS_CONTEXT_KEYS = ('distro', 'arch', 'initiator')
TMT_INITIATOR = 'albs'
TMT_ARCH_MAPPING = {
    'x86_64_v2': 'x86_64',
    'amd64': 'x86_64',
    'arm64': 'aarch64',
}


@dataclass
class TmtResult:
    name: str
    plan: str
    result: str
    note: str = ''
    duration: str = ''
    logs: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.result in SUCCESS_OUTCOMES

    @property
    def skipped(self) -> bool:
        return self.result in SKIP_OUTCOMES


@dataclass(frozen=True)
class DiscoveredTest:
    """
    A test of discover/tests.yaml of a plan.

    In a plan with several discover phases tmt names a test
    /<phase>/<name> in the results, but "tmt run tests --name" matches
    the name without the phase prefix (tmt applies it inside the phase,
    before adding the prefix).
    """

    # As in the results
    name: str
    # discover phase of the test
    phase: str = ''

    @property
    def filter_name(self) -> str:
        """The name "tmt run tests --name" sees: without the phase prefix."""
        prefix = f'/{self.phase}/'
        if self.phase and self.name.startswith(prefix):
            return self.name[len(prefix) - 1:]
        return self.name


def get_tmt_test_key(name: str) -> str:
    """Name of a tmt test in the result and in skipped_tests."""
    return f'{TMT_TEST_PREFIX}{name}'


def glob_to_regex(pattern: str) -> str:
    """
    Translates a shell pattern of skipped_tests into a regular
    expression without anchors.

    fnmatch.translate() isn't used because its result depends on the
    Python version and the regex is matched by tmt in the environment,
    on Python 3.6 on AlmaLinux 8.
    """
    result = []
    i = 0
    length = len(pattern)
    while i < length:
        char = pattern[i]
        i += 1
        if char == '*':
            result.append('.*')
        elif char == '?':
            result.append('.')
        elif char == '[':
            end = i
            if end < length and pattern[end] == '!':
                end += 1
            if end < length and pattern[end] == ']':
                end += 1
            while end < length and pattern[end] != ']':
                end += 1
            if end >= length:
                result.append(re.escape(char))
                continue
            chars = pattern[i:end].replace('\\', '\\\\')
            i = end + 1
            if chars.startswith('!'):
                chars = '^' + chars[1:]
            elif chars.startswith('^'):
                chars = '\\' + chars
            result.append(f'[{chars}]')
        else:
            result.append(re.escape(char))
    return ''.join(result)


def get_skip_patterns(skipped_tests: Iterable[str]) -> List[str]:
    """Shell patterns of the tmt test names from skipped_tests."""
    return [
        test[len(TMT_TEST_PREFIX):]
        for test in skipped_tests
        if test.startswith(TMT_TEST_PREFIX) and len(test) > len(TMT_TEST_PREFIX)
    ]


def build_test_name_regex(
    skipped_tests: Iterable[str],
    discovered: Iterable[DiscoveredTest] = (),
) -> Optional[str]:
    """
    Regex for "tmt run tests --name" that selects every test except the
    skipped ones. The skipped tests have to be left out of the tmt run,
    not only out of its result: a test calling reboot aborts the whole
    run (ALTS-5, CFG-6). tmt 1.26 (EPEL 8) has no "tests --exclude",
    so it's a negative lookahead. tmt matches it with re.search()
    against the test name.

    A skip rule may name a test as the results do, with the discover
    phase prefix (/cups-public/Sanity/...), which "tests --name" doesn't
    see: such a test of the discovered ones is added by the name
    without the prefix.
    """
    skipped_tests = list(skipped_tests)
    patterns = get_skip_patterns(skipped_tests)
    if not patterns:
        return None
    alternatives = [glob_to_regex(pattern) for pattern in patterns]
    for test in discovered:
        if test.filter_name == test.name:
            continue
        alternative = re.escape(test.filter_name)
        if alternative not in alternatives and is_tmt_test_skipped(
            test.name,
            skipped_tests,
            test.filter_name,
        ):
            alternatives.append(alternative)
    return f'^(?!(?:{"|".join(alternatives)})$)'


def get_excluded_tests(
    discovered: Iterable[DiscoveredTest],
    test_name_regex: Optional[str],
) -> List[DiscoveredTest]:
    """Discovered tests "tmt run tests --name REGEX" leaves out."""
    if not test_name_regex:
        return []
    regex = re.compile(test_name_regex)
    return [test for test in discovered if not regex.search(test.filter_name)]


def is_tmt_test_skipped(
    name: str,
    skipped_tests: Iterable[str],
    filter_name: Optional[str] = None,
) -> bool:
    """
    Whether a skip rule matches the test: by the name of the results or
    by the name without the discover phase prefix (filter_name), so a
    rule may use either of them.
    """
    keys = {get_tmt_test_key(name)}
    if filter_name:
        keys.add(get_tmt_test_key(filter_name))
    return any(
        fnmatch.fnmatchcase(key, test)
        for test in skipped_tests
        if test.startswith(TMT_TEST_PREFIX)
        for key in keys
    )


def parse_os_release(text: str) -> Dict[str, str]:
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        result[key.strip()] = value.strip().strip('"\'')
    return result


def build_distro_context(os_release: Dict[str, str], dist_version: str) -> str:
    """
    Value of the tmt "distro" context (D-8, D-22, ALTS-14):
    almalinux-9.7,centos-stream-9,rhel-9.7 for AlmaLinux 9.7 and
    almalinux-kitten-10,centos-stream-10 for Kitten, which is ahead of
    RHEL 10.x and has no RHEL minor version.
    """
    version_id = os_release.get('VERSION_ID') or str(dist_version)
    major = version_id.split('.')[0]
    names = ' '.join((
        str(dist_version),
        os_release.get('NAME', ''),
        os_release.get('VERSION', ''),
    )).lower()
    if 'kitten' in names:
        return f'almalinux-kitten-{major},centos-stream-{major}'
    return f'almalinux-{version_id},centos-stream-{major},rhel-{version_id}'


def build_tmt_context(
    os_release: Dict[str, str],
    dist_version: str,
    dist_arch: str,
    extra_context: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, str], List[str]]:
    """
    tmt context: distro, arch and initiator set by ALTS, then the context
    from ALBS (component and the context of alts.yaml).

    Returns the context and the keys of ALBS that were ignored because
    ALTS sets them itself.
    """
    context = {
        'distro': build_distro_context(os_release, dist_version),
        'arch': TMT_ARCH_MAPPING.get(dist_arch, dist_arch),
        'initiator': TMT_INITIATOR,
    }
    ignored = []
    for key, value in (extra_context or {}).items():
        if key in ALTS_CONTEXT_KEYS:
            ignored.append(key)
            continue
        if isinstance(value, (list, tuple)):
            value = ','.join(str(item) for item in value)
        context[key] = str(value)
    return context, ignored


def plan_name_regex(plan: str) -> str:
    return f'^{re.escape(plan)}$'


def build_tmt_command(
    plans: List[str],
    context: Dict[str, str],
    environment: Optional[Dict[str, str]] = None,
    test_filter: Optional[str] = None,
    test_name_regex: Optional[str] = None,
    feeling_safe: bool = True,
    run_dir: str = TMT_RUN_DIR,
    timeout: Optional[int] = None,
) -> List[str]:
    """
    tmt [--feeling-safe] -c ... run --id DIR -e HOME=/root -e PATH=... --all
        provision --how local plans --name ^PLAN$ ...
        [tests [--filter FILTER] [--name REGEX]]

    --feeling-safe: tmt >= 1.77 refuses "provision --how local" without
    it, tmt 1.26 from EPEL 8 doesn't know it (ALTS-6).
    """
    cmd = []
    if timeout:
        cmd.extend(['timeout', '--kill-after=60', str(timeout)])
    cmd.append('tmt')
    if feeling_safe:
        cmd.append('--feeling-safe')
    for key, value in context.items():
        cmd.extend(['-c', f'{key}={value}'])
    cmd.extend(['run', '--id', run_dir])
    for key, value in (environment or {}).items():
        if key in TMT_ENVIRONMENT:
            continue
        cmd.extend(['-e', f'{key}={value}'])
    for key, value in TMT_ENVIRONMENT.items():
        cmd.extend(['-e', f'{key}={value}'])
    cmd.extend(['--all', 'provision', '--how', 'local', 'plans'])
    for plan in plans:
        cmd.extend(['--name', plan_name_regex(plan)])
    if test_filter or test_name_regex:
        cmd.append('tests')
        if test_filter:
            cmd.extend(['--filter', test_filter])
        if test_name_regex:
            cmd.extend(['--name', test_name_regex])
    return cmd


def _iter_strings(data: Any) -> Iterable[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for value in data.values():
            yield from _iter_strings(value)
    elif isinstance(data, (list, tuple)):
        for value in data:
            yield from _iter_strings(value)


def find_internal_plans(
    plans: List[str],
    exported_plans: Optional[List[dict]] = None,
) -> Dict[str, str]:
    """
    Plans that need Red Hat infrastructure and are not run (ALTS-14):
    named .../internal or discovering/importing tests from internal
    Red Hat repositories. Returns {plan: reason}.

    exported_plans is the output of "tmt plans export".
    """
    exported = {
        plan.get('name'): plan
        for plan in (exported_plans or [])
        if isinstance(plan, dict)
    }
    result = {}
    for plan in plans:
        if INTERNAL_PLAN_NAME_RE.search(plan):
            result[plan] = 'internal plan'
            continue
        data = exported.get(plan)
        if not data:
            continue
        sources = [data.get('discover'), data.get('plan')]
        for value in _iter_strings(sources):
            marker = next(
                (marker for marker in INTERNAL_PLAN_MARKERS if marker in value),
                None,
            )
            if marker:
                result[plan] = f'uses internal Red Hat repository {value}'
                break
    return result


def _load_yaml_list(text: str) -> List[dict]:
    data = yaml.safe_load(text) if text else None
    if not data:
        return []
    if isinstance(data, dict):
        data = [data]
    return [item for item in data if isinstance(item, dict)]


def parse_exported_plans(text: str) -> List[dict]:
    return _load_yaml_list(text)


def parse_discovered_tests(text: str) -> List[DiscoveredTest]:
    """
    Tests of discover/tests.yaml of a plan. The discover phase is
    discover-phase in recent tmt (1.78) and discover_phase in tmt 1.26.
    """
    return [
        DiscoveredTest(
            name=str(test['name']),
            phase=str(
                test.get('discover-phase') or test.get('discover_phase') or ''
            ),
        )
        for test in _load_yaml_list(text)
        if test.get('name')
    ]


def parse_results(text: str, plan: str) -> List[TmtResult]:
    """Results from execute/results.yaml of a plan."""
    results = []
    for item in _load_yaml_list(text):
        name = item.get('name')
        if not name:
            continue
        logs = item.get('log') or []
        if isinstance(logs, str):
            logs = [logs]
        results.append(
            TmtResult(
                name=str(name),
                plan=plan,
                result=str(item.get('result') or 'error').lower(),
                note=_note_to_text(item.get('note')),
                duration=str(item.get('duration') or ''),
                logs=[str(log) for log in logs],
            )
        )
    return results


def _note_to_text(note: Any) -> str:
    if not note:
        return ''
    if isinstance(note, (list, tuple)):
        return '; '.join(str(item) for item in note)
    return str(note)


def get_output_log(result: TmtResult) -> Optional[str]:
    for log in result.logs:
        if Path(log).name == 'output.txt':
            return log
    return result.logs[0] if result.logs else None


def read_output_tail(text: str, lines: int = 20, limit: int = 2000) -> str:
    tail = '\n'.join(text.rstrip('\n').splitlines()[-lines:])
    if len(tail) > limit:
        tail = tail[-limit:]
    return tail


def make_result_keys(results: List[TmtResult]) -> List[str]:
    """
    Result keys: tmt_test_<name>; a test that is in more than one of the
    selected plans gets the plan appended: tmt_test_<name>@<plan>.
    """
    plans_by_name: Dict[str, set] = {}
    for result in results:
        plans_by_name.setdefault(result.name, set()).add(result.plan)
    keys = []
    for result in results:
        key = get_tmt_test_key(result.name)
        if len(plans_by_name[result.name]) > 1:
            key = f'{key}@{result.plan}'
        keys.append(key)
    return keys


def format_result_summary(result: TmtResult, output_tail: str = '') -> str:
    lines = [
        f'Result: {result.result}',
        f'Plan: {result.plan}',
    ]
    if result.duration:
        lines.append(f'Duration: {result.duration}')
    if result.note:
        lines.append(f'Note: {result.note}')
    if output_tail:
        lines.extend(['', output_tail])
    return '\n'.join(lines)


def count_outcomes(results: Iterable[TmtResult]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results:
        counts[result.result] = counts.get(result.result, 0) + 1
    return counts


def parse_step_status(text: str) -> Optional[str]:
    """
    Status of a step of a plan from its <step>/step.yaml: "done" once
    the step has finished, "todo" while it runs or after it has failed
    (tmt 1.26 and 1.78 alike).
    """
    data = yaml.safe_load(text) if text else None
    if not isinstance(data, dict):
        return None
    status = data.get('status')
    return str(status) if status else None


ANSI_ESCAPE_REGEX = re.compile(r'\x1b\[[0-9;]*m')


def extract_step_errors(
    output: str,
    plan: str,
    step: str = 'discover',
    limit: int = 10,
) -> List[str]:
    """
    Errors of a failed step of a plan in the tmt output: the "fail:"
    lines of the step (tmt 1.78) and the causes tmt prints at the end
    of the run, which stops at the failed plan (the only place tmt 1.26
    has the error in):

    /plans/tier1/public
        discover
            how: fmf
            fail: Command 'git clone https://...' returned 128.
        report
    plan failed.
    The exception was caused by 1 earlier exceptions
    Cause number 1:
        Command 'git clone https://...' returned 128.
        stderr (3/3 lines)
        ~~~~~~~~
        fatal: Authentication failed for 'https://...'

    The first line of a cause and the "fatal:" lines of git in it are
    taken; "warn:" lines (fmf schema warnings, ...) are not errors.
    """
    errors: List[str] = []

    def add(error: str):
        if error and error not in errors:
            errors.append(error)

    lines = ANSI_ESCAPE_REGEX.sub('', output).splitlines()
    in_plan = in_step = False
    step_indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_plan = stripped == plan
            in_step = False
            continue
        if not in_plan:
            continue
        if stripped == step:
            in_step, step_indent = True, indent
            continue
        if in_step and indent <= step_indent:
            in_step = False
        if in_step and stripped.startswith(('fail:', 'Error:')):
            add(stripped)
    fail_texts = {error.split(':', 1)[1].strip() for error in errors}
    in_causes = cause_first_line = False
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r'Cause number \d+:', stripped):
            in_causes = cause_first_line = True
            continue
        if not stripped or not in_causes:
            continue
        if cause_first_line:
            cause_first_line = False
            if (
                stripped not in fail_texts
                and not stripped.startswith('The exception was caused')
            ):
                add(stripped)
            continue
        if stripped.startswith('fatal:'):
            add(stripped)
    return errors[:limit]
