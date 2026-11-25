from typing import Dict, List, Optional

import pytest

from alts.worker import tasks
from alts.worker.tasks import should_skip_test


class FakeRunner:
    """Records the test stages run_tests() runs instead of running them."""

    def __init__(self, *args, **kwargs):
        self.stages = []
        self.received_skipped_tests = None
        self.artifacts = {}
        self.uploaded_logs = None
        self.stats = {}
        self.skipped_third_party_tests = []
        self.skipped_tmt_tests = []
        self.tmt_report = {}
        self.kwargs = kwargs

    def setup(self):
        pass

    def teardown(self):
        pass

    def run_system_info_commands(self):
        self.stages.append('system_info')

    def install_package(self, *args, **kwargs):
        self.stages.append('install_package')

    def run_package_integrity_tests(self, *args, **kwargs):
        self.stages.append('package_integrity_tests')

    def run_third_party_tests(
        self,
        *args,
        skipped_tests: Optional[List[str]] = None,
        **kwargs,
    ):
        self.stages.append('third_party_tests')
        self.received_skipped_tests = skipped_tests
        self.skipped_third_party_tests = ['third_party_test_smoke.sh']

    def uninstall_package(self, *args, **kwargs):
        self.stages.append('uninstall_package')

    def run_tmt_tests(
        self,
        package_tests: dict,
        skipped_tests: Optional[List[str]] = None,
    ):
        self.stages.append('tmt_tests')
        self.received_skipped_tests = skipped_tests
        self.skipped_tmt_tests = ['tmt_test_/Sanity/reboot']
        self.tmt_report = {'commit': package_tests['commit'], 'counts': {}}
        self.artifacts['tmt_tests'] = {
            'exit_code': 0,
            'stdout': 'tmt\n1..1\nnot ok 1 - looks like TAP',
            'stderr': '',
        }
        self.artifacts['tmt'] = {
            'tmt_test_/Sanity/broken': {
                'exit_code': 1,
                'stdout': 'full output',
                'stderr': '',
                'tmt': {
                    'name': '/Sanity/broken',
                    'plan': '/plans/tier1/public',
                    'result': 'fail',
                    'duration': '00:00:01',
                    'note': '',
                    'summary': 'Result: fail',
                },
            },
        }


@pytest.mark.parametrize(
    'package_name, test_name, excluded_tests, skipped_tests, expected',
    [
        pytest.param(
            'foo',
            'install_package',
            {},
            [],
            False,
            id='nothing-skipped',
        ),
        pytest.param(
            'foo',
            'uninstall_package',
            {},
            ['uninstall_package'],
            True,
            id='skipped-for-task',
        ),
        pytest.param(
            'foo',
            'run_package_integrity_tests',
            {},
            ['package_integrity_tests'],
            True,
            id='skipped-integrity-tests',
        ),
        pytest.param(
            'foo',
            'run_third_party_tests',
            {},
            ['third_party_tests'],
            True,
            id='skipped-third-party-tests',
        ),
        pytest.param(
            'foo',
            'run_third_party_tests',
            {},
            ['third_party_test_smoke.sh'],
            False,
            id='single-third-party-test-skipped',
        ),
        pytest.param(
            'foo-devel',
            'run_package_integrity_tests',
            {'foo': ['run_package_integrity_tests']},
            [],
            True,
            id='excluded-for-package',
        ),
        pytest.param(
            'bar',
            'run_package_integrity_tests',
            {'foo': ['run_package_integrity_tests']},
            [],
            False,
            id='excluded-for-other-package',
        ),
        pytest.param(
            'foo',
            'run_third_party_tests',
            {'foo': ['run_third_party_tests']},
            [],
            True,
            id='excluded-third-party-tests',
        ),
    ],
)
def test_should_skip_test(
    package_name: str,
    test_name: str,
    excluded_tests: Dict[str, List[str]],
    skipped_tests: List[str],
    expected: bool,
):
    assert (
        should_skip_test(
            package_name,
            test_name,
            excluded_tests,
            skipped_tests,
        )
        is expected
    )


@pytest.mark.parametrize(
    'excluded_tests, skipped_tests, stages, summary_skipped_tests',
    [
        pytest.param(
            {},
            None,
            [
                'system_info',
                'install_package',
                'package_integrity_tests',
                'third_party_tests',
                'uninstall_package',
            ],
            ['third_party_test_smoke.sh'],
            id='nothing-skipped',
        ),
        pytest.param(
            {},
            ['package_integrity_tests', 'uninstall_package'],
            ['system_info', 'install_package', 'third_party_tests'],
            [
                'package_integrity_tests',
                'uninstall_package',
                'third_party_test_smoke.sh',
            ],
            id='skipped-for-task',
        ),
        pytest.param(
            {'foo': ['run_third_party_tests']},
            ['uninstall_package'],
            ['system_info', 'install_package', 'package_integrity_tests'],
            ['third_party_tests', 'uninstall_package'],
            id='excluded-and-skipped',
        ),
    ],
)
def test_run_tests_skips_tests(
    monkeypatch,
    excluded_tests: Dict[str, List[str]],
    skipped_tests: Optional[List[str]],
    stages: List[str],
    summary_skipped_tests: List[str],
):
    runners = []

    def create_runner(*args, **kwargs):
        runners.append(FakeRunner(*args, **kwargs))
        return runners[-1]

    monkeypatch.setitem(tasks.RUNNER_MAPPING, 'docker', create_runner)
    monkeypatch.setattr(tasks.time, 'sleep', lambda _: None)
    monkeypatch.setattr(tasks.CONFIG, 'enable_integrity_tests', True)
    summary = tasks.run_tests.run(
        {
            'task_id': 'task_id',
            'runner_type': 'docker',
            'dist_name': 'almalinux',
            'dist_version': '9',
            'dist_arch': 'x86_64',
            'repositories': [],
            'package_name': 'foo',
            'skipped_tests': skipped_tests,
        },
        excluded_tests,
    )
    runner = runners[0]
    assert runner.stages == stages
    if 'third_party_tests' in stages:
        assert runner.received_skipped_tests == (skipped_tests or [])
    assert summary['skipped_tests'] == summary_skipped_tests


PACKAGE_TESTS = {
    'url': 'https://git.almalinux.org/tests/chrony.git',
    'commit': 'a' * 40,
    'tmt': {'plans': ['/plans/tier1/public']},
}


@pytest.mark.parametrize(
    'skipped_tests, stages',
    [
        pytest.param(
            ['tmt_test_/Sanity/reboot'],
            ['system_info', 'tmt_tests'],
            id='tmt-task',
        ),
        pytest.param(['tmt_tests'], ['system_info'], id='tmt-tests-skipped'),
    ],
)
def test_run_tests_tmt_task(
    monkeypatch,
    skipped_tests: List[str],
    stages: List[str],
):
    runners = []

    def create_runner(*args, **kwargs):
        runners.append(FakeRunner(*args, **kwargs))
        return runners[-1]

    monkeypatch.setitem(tasks.RUNNER_MAPPING, 'docker', create_runner)
    monkeypatch.setattr(tasks.time, 'sleep', lambda _: None)
    monkeypatch.setattr(
        tasks.CONFIG.logs_uploader_config,
        'skip_artifacts_upload',
        False,
    )
    summary = tasks.run_tests.run(
        {
            'task_id': 'task_id',
            'runner_type': 'docker',
            'dist_name': 'almalinux',
            'dist_version': '9',
            'dist_arch': 'x86_64',
            'repositories': [],
            'package_name': 'chrony',
            'skipped_tests': skipped_tests,
            'package_tests': PACKAGE_TESTS,
        },
        {},
    )
    runner = runners[0]
    assert runner.kwargs['package_tests'] == PACKAGE_TESTS
    # A component task doesn't run the stages of a single package
    assert runner.stages == stages
    if 'tmt_tests' not in stages:
        assert summary['skipped_tests'] == ['tmt_tests']
        return
    assert runner.received_skipped_tests == skipped_tests
    assert summary['skipped_tests'] == ['tmt_test_/Sanity/reboot']
    # The stage output is not taken for TAP
    assert summary['tmt_tests'] == {
        'success': True,
        'commit': 'a' * 40,
        'counts': {},
    }
    assert summary['tmt'] == {
        'tmt_test_/Sanity/broken': {
            'success': False,
            'output': 'Result: fail',
            'name': '/Sanity/broken',
            'plan': '/plans/tier1/public',
            'result': 'fail',
            'duration': '00:00:01',
            'note': '',
        },
    }
