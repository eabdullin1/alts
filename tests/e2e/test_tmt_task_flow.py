"""
Flow tests of a whole ALTS task, from the payload ALBS returns from
get_test_tasks to the result ALTS posts back to ALBS.

They repeat the live check ALBS -> ALTS -> ALBS on the albs-test stand
(build of grep, AlmaLinux 9 x86_64, tests/grep at the head of main),
see tests/fixtures/tmt/README.md. ALTS runs its real code: scheduler,
worker task, Docker runner, the Terraform template, the tmt stage and
the result; only docker, terraform, ansible-playbook and git are fake
(tests/e2e/fake_tools.py), so no container is started.
"""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml
from plumbum import local

from alts.scheduler import CONFIG as SCHEDULER_CONFIG
from alts.scheduler import scheduling
from alts.shared.models import TaskRequestPayload
from alts.worker import CONFIG
from alts.worker import tasks
from alts.worker.runners import base as base_runner
from alts.worker.runners import docker as docker_runner

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'tmt'
FAKE_TOOLS = Path(__file__).with_name('fake_tools.py')
TMT_PATH = 'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
INIT_CONTAINER = {
    'cgroupns_mode': 'private',
    'security_opts': ['writable-cgroups=true'],
    'tmpfs': ['/run', '/run/lock'],
    'command': None,
    'privileged': False,
}


def load_payload(scenario: str) -> dict:
    return json.loads(Path(FIXTURES, scenario, 'payload.json').read_text())


class FakeSession:
    """requests.Session of the worker, collects the results sent to ALBS."""

    posted: List[dict] = []

    def mount(self, *args):
        pass

    def post(self, url, json=None, headers=None, timeout=None):
        self.posted.append({'url': url, 'json': json, 'headers': headers})
        return self

    def raise_for_status(self):
        pass


class FakeUploader:
    uploaded: List[str] = []

    def __init__(self, *args, **kwargs):
        pass

    def upload(self, artifacts_dir: str, upload_dir: str, **kwargs):
        names = sorted(os.listdir(artifacts_dir))
        self.uploaded.extend(names)
        return [{'name': name, 'href': f'/pulp/{name}'} for name in names]

    def upload_single_file(self, filename: str, type_: str = 'test_log'):
        name = os.path.basename(filename)
        self.uploaded.append(name)
        return {'name': name, 'href': f'/pulp/{name}', 'type': type_}


class DbSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def begin(self):
        return self

    def add(self, record):
        pass

    def commit(self):
        pass


class Flow:
    """One ALTS task run through the scheduler and the worker."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.state = Path(tmp_path, 'state')
        self.bin = Path(tmp_path, 'bin')
        self.bin.mkdir()
        for tool in ('docker', 'terraform', 'ansible-playbook', 'git'):
            wrapper = Path(self.bin, tool)
            wrapper.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TOOLS}" '
                f'{tool} "$@"\n'
            )
            wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        cgroup_controllers = Path(tmp_path, 'cgroup.controllers')
        cgroup_controllers.write_text('cpu memory pids\n')
        work_dir = Path(tmp_path, 'work')
        work_dir.mkdir()
        FakeSession.posted = []
        FakeUploader.uploaded = []
        self.scheduled: List[tuple] = []

        monkeypatch.setattr(tempfile, 'tempdir', str(work_dir))
        monkeypatch.setattr(
            base_runner, 'TF_INIT_LOCK_PATH', str(tmp_path / 'tf_lock'),
        )
        monkeypatch.setattr(
            docker_runner,
            'CGROUP_V2_CONTROLLERS_FILE',
            str(cgroup_controllers),
        )
        monkeypatch.setattr(base_runner, 'PulpLogsUploader', FakeUploader)
        monkeypatch.setattr(
            CONFIG.logs_uploader_config, 'skip_artifacts_upload', False,
        )
        monkeypatch.setattr(tasks.requests, 'Session', FakeSession)
        monkeypatch.setattr(tasks.time, 'sleep', lambda _: None)
        monkeypatch.setattr(
            tasks.run_tests,
            'is_aborted',
            lambda *args, **kwargs: False,
            raising=False,
        )
        monkeypatch.setattr(SCHEDULER_CONFIG, 'supported_runners', 'all')
        monkeypatch.setattr(SCHEDULER_CONFIG, 'queue_arch_overrides', [])
        monkeypatch.setattr(scheduling, 'Session', DbSession)
        monkeypatch.setattr(
            scheduling.run_tests,
            'apply_async',
            lambda args, **kwargs: self.scheduled.append((args, kwargs)),
        )

    def schedule(self, payloads: List[dict]) -> List[tuple]:
        """The scheduler loop over the tasks ALBS returned."""
        scheduler = scheduling.TestsScheduler(
            __import__('threading').Event(),
            __import__('threading').Event(),
            celery_app=None,
        )
        self.monkeypatch.setattr(
            scheduler, 'get_available_test_tasks', lambda: payloads,
        )
        self.monkeypatch.setattr(
            scheduler, 'get_excluded_packages', lambda distro: {},
        )

        def stop(_):
            scheduler._TestsScheduler__graceful_terminate.set()
            scheduler._TestsScheduler__terminated_event.set()

        self.monkeypatch.setattr(scheduling.time, 'sleep', stop)
        scheduler.run()
        return self.scheduled

    def run(self, scenario: Path, payload: dict) -> dict:
        """Schedules the task and runs it in the worker, returns its result."""
        scheduled = self.schedule([payload])
        assert len(scheduled) == 1
        (task_params, excluded_tests), kwargs = scheduled[0]
        self.task_id = kwargs['task_id']
        self.queue = kwargs['queue']
        with local.env(
            PATH=f'{self.bin}{os.pathsep}{local.env["PATH"]}',
            FAKE_ALTS_STATE=str(self.state),
            FAKE_ALTS_SCENARIO=str(scenario),
        ):
            summary = tasks.run_tests.run(task_params, excluded_tests)
        assert len(FakeSession.posted) == 1
        self.callback = FakeSession.posted[0]
        assert self.callback['json']['result'] == summary
        return summary

    def calls(self, tool: str) -> List[dict]:
        path = Path(self.state, 'calls.jsonl')
        if not path.exists():
            return []
        calls = [json.loads(line) for line in path.read_text().splitlines()]
        return [call for call in calls if call['tool'] == tool]

    def tmt_runs(self, discover_only: bool = False) -> List[dict]:
        return [
            call for call in self.calls('tmt')
            if call['discover_only'] is discover_only
        ]

    def containers(self) -> Dict[str, dict]:
        return {
            path.name: json.loads(Path(path, 'config.json').read_text())
            for path in Path(self.state, 'containers').iterdir()
        }

    def provision_variables(self) -> str:
        provisions = [
            call for call in self.calls('ansible-playbook')
            if call['tags'] == 'initial_provision'
        ]
        assert len(provisions) == 1
        return provisions[0]['variables']


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flow:
    return Flow(tmp_path, monkeypatch)


def scenario_copy(tmp_path: Path, scenario: str, **changes) -> Path:
    path = Path(tmp_path, 'scenario', scenario)
    shutil.copytree(Path(FIXTURES, scenario), path)
    scenario_file = Path(path, 'scenario.yaml')
    data = yaml.safe_load(scenario_file.read_text())
    data.update(changes)
    scenario_file.write_text(yaml.safe_dump(data))
    return path


def normalize(result: dict) -> dict:
    """The result without the log references, they carry the task ID."""
    return {key: value for key, value in result.items() if key != 'logs'}


def assert_init_container(container: dict, image: str):
    assert container['image'] == image
    for key, value in INIT_CONTAINER.items():
        assert container[key] == value, key
    assert container['destroyed']


def test_grep_component_task(flow: Flow):
    """The live check: tests/grep plans on AlmaLinux 9 x86_64."""
    payload = load_payload('grep-al9')
    result = flow.run(FIXTURES / 'grep-al9', payload)

    # The task reaches the worker with the whole package_tests
    assert flow.queue == 'docker-x86_64-0'
    assert flow.callback['url'] == (
        'http://web_server:8000/api/v1/tests/1001/result/'
    )
    assert flow.callback['json']['api_version'] == '20210512'

    # Init container, tmt is installed in the provision
    (container,) = flow.containers().values()
    assert_init_container(container, 'almalinux/9-init:9')
    variables = flow.provision_variables()
    assert "'tmt_is_needed': True" in variables
    assert "'pytest_is_needed': False" in variables
    assert '/var/tmp/tmt-tests/grep' in [
        call['args'][2].split(':', 1)[1] for call in flow.calls('docker')
        if call['args'][0] == 'cp' and ':' in call['args'][2]
    ]

    # tmt command: plans of the payload, its context, --feeling-safe
    (tmt_run,) = flow.tmt_runs()
    assert tmt_run['workdir'] == '/var/tmp/tmt-tests/grep'
    assert tmt_run['feeling_safe'] is True
    assert tmt_run['context'] == {
        'distro': 'almalinux-9.9,centos-stream-9,rhel-9.9',
        'arch': 'x86_64',
        'initiator': 'albs',
        'trigger': 'build',
        'component': 'grep',
    }
    assert tmt_run['environment'] == ['HOME=/root', TMT_PATH]
    assert tmt_run['plan_regexes'] == [
        r'^/plans/tier1/public$',
        r'^/plans/tier2\-tier3/public$',
        r'^/plans/others/public$',
    ]
    assert tmt_run['test_regexes'] == []
    # Nothing skipped: no separate discover run
    assert flow.tmt_runs(discover_only=True) == []

    # Result for ALBS
    assert result['skipped_tests'] == []
    for stage in ('initialize_terraform', 'start_environment',
                  'initial_provision', 'system_info', 'stop_environment'):
        assert result[stage] == {'success': True}, stage
    for stage in ('install_package', 'package_integrity_tests',
                  'third_party_tests_wrapper', 'uninstall_package'):
        assert stage not in result
    tmt_tests = result['tmt_tests']
    assert tmt_tests['success'] is True
    assert tmt_tests['tmt_exit_code'] == 1
    assert tmt_tests['counts'] == {'pass': 25, 'fail': 2}
    assert tmt_tests['plans'] == [
        {'name': '/plans/tier1/public', 'counts': {'pass': 6}},
        {'name': '/plans/tier2-tier3/public',
         'counts': {'pass': 19, 'fail': 2}},
        {'name': '/plans/others/public', 'counts': {}},
    ]
    assert tmt_tests['excluded_plans'] == []
    assert tmt_tests['installed_packages'] == ['grep-3.6-5.el9.x86_64']
    assert tmt_tests['commit'] == payload['package_tests']['commit']
    assert len(result['tmt']) == 27
    failed = {
        key: value for key, value in result['tmt'].items()
        if not value['success']
    }
    assert sorted(failed) == [
        'tmt_test_/Regression/bz1050916-malformed-output-when-coloring-is-on',
        'tmt_test_/Regression/inconsistent-w-and-alnum-behaviour',
    ]
    for key, value in failed.items():
        assert value['name'] == key[len('tmt_test_'):]
        assert value['plan'] == '/plans/tier2-tier3/public'
        assert value['result'] == 'fail'
        assert value['output'].startswith('Result: fail\n')
        assert 'RESULT: ' + value['name'] in value['output']
    assert result['tmt']['tmt_test_/Sanity/smoke-test']['plan'] == (
        '/plans/tier1/public'
    )

    # Logs of the tests, with the task ID and without '/'
    assert (
        'tmt_test__Regression_inconsistent-w-and-alnum-behaviour_'
        f'{flow.task_id}.log'
    ) in FakeUploader.uploaded
    assert result['logs']

    # The whole result ALBS gets, the format for the ALBS side
    # ALTS_UPDATE_EXPECTED=1 rewrites it after an intended format change
    expected_file = Path(FIXTURES, 'grep-al9', 'expected_result.json')
    if os.environ.get('ALTS_UPDATE_EXPECTED'):
        expected_file.write_text(
            json.dumps(normalize(result), indent=2, sort_keys=True) + '\n'
        )
    assert normalize(result) == json.loads(expected_file.read_text())


@pytest.mark.parametrize(
    'dist_version, distro, feeling_safe, image',
    [
        pytest.param(
            '9', 'almalinux-9.9,centos-stream-9,rhel-9.9', True,
            'almalinux/9-init:9', id='almalinux-9',
        ),
        pytest.param(
            '8', 'almalinux-8.10,centos-stream-8,rhel-8.10', False,
            'almalinux/8-init:8', id='almalinux-8-tmt-1.26',
        ),
        pytest.param(
            '10-kitten', 'almalinux-kitten-10,centos-stream-10', True,
            'almalinux/10-kitten-init:10-kitten', id='kitten',
        ),
    ],
)
def test_synthetic_plans(
    flow: Flow,
    dist_version: str,
    distro: str,
    feeling_safe: bool,
    image: str,
):
    """
    Synthetic plans of the live check: a skipped test, a test of two
    plans, warn, a test tmt skips itself, and an internal plan.
    """
    payload = load_payload('e2e-plans')
    payload['dist_version'] = dist_version
    result = flow.run(FIXTURES / 'e2e-plans', payload)

    (container,) = flow.containers().values()
    assert_init_container(container, image)

    # The skipped test is left out of the tmt run itself
    (discover,) = flow.tmt_runs(discover_only=True)
    assert discover['test_regexes'] == []
    (tmt_run,) = flow.tmt_runs()
    assert tmt_run['feeling_safe'] is feeling_safe
    assert tmt_run['context']['distro'] == distro
    assert tmt_run['plan_regexes'] == [
        r'^/plans/e2e/gating$',
        r'^/plans/e2e/info$',
    ]
    assert tmt_run['test_regexes'] == [r'^(?!(?:/e2e/skipme)$)']
    assert tmt_run['executed'] == {
        '/plans/e2e/gating': ['/e2e/pass', '/e2e/fail'],
        '/plans/e2e/info': ['/e2e/pass', '/e2e/warn', '/e2e/skipped-by-tmt'],
    }

    assert result['skipped_tests'] == [
        'tmt_test_/e2e/skipme',
        'tmt_test_/e2e/skipped-by-tmt',
    ]
    assert {
        key: (value['result'], value['success'], value['name'], value['plan'])
        for key, value in result['tmt'].items()
    } == {
        'tmt_test_/e2e/pass@/plans/e2e/gating': (
            'pass', True, '/e2e/pass', '/plans/e2e/gating',
        ),
        'tmt_test_/e2e/fail': ('fail', False, '/e2e/fail', '/plans/e2e/gating'),
        'tmt_test_/e2e/pass@/plans/e2e/info': (
            'pass', True, '/e2e/pass', '/plans/e2e/info',
        ),
        'tmt_test_/e2e/warn': ('warn', True, '/e2e/warn', '/plans/e2e/info'),
    }
    assert 'Note: fail in the test phase' in (
        result['tmt']['tmt_test_/e2e/fail']['output']
    )
    tmt_tests = result['tmt_tests']
    assert tmt_tests['success'] is True
    assert tmt_tests['tmt_exit_code'] == 1
    assert tmt_tests['counts'] == {'pass': 2, 'fail': 1, 'warn': 1}
    assert tmt_tests['plans'] == [
        {'name': '/plans/e2e/gating', 'counts': {'pass': 1, 'fail': 1}},
        {'name': '/plans/e2e/info',
         'counts': {'pass': 1, 'warn': 1, 'skip': 1}},
    ]
    assert tmt_tests['excluded_plans'] == [
        {'name': '/plans/e2e/internal', 'reason': 'internal plan'},
    ]


def test_reboot_stops_container(flow: Flow, tmp_path: Path):
    """
    A test reboots the init container: tmt dies with it, the results
    tmt has written are still collected and sent to ALBS.
    """
    scenario = scenario_copy(tmp_path, 'e2e-plans', reboot='/e2e/pass')
    result = flow.run(scenario, load_payload('e2e-plans'))

    (container,) = flow.containers().values()
    assert container['running'] is False
    assert container['destroyed']
    assert result['tmt'] == {
        'tmt_test_/e2e/pass': {
            'success': True,
            'output': result['tmt']['tmt_test_/e2e/pass']['output'],
            'name': '/e2e/pass',
            'plan': '/plans/e2e/gating',
            'result': 'pass',
            'duration': '00:00:01',
            'note': '',
        },
        'tmt_test_/e2e/fail': {
            'success': False,
            'output': (
                'Result: pending\nPlan: /plans/e2e/gating\n'
                'Duration: 00:00:01\nNote: fail in the test phase'
            ),
            'name': '/e2e/fail',
            'plan': '/plans/e2e/gating',
            'result': 'pending',
            'duration': '00:00:01',
            'note': 'fail in the test phase',
        },
    }
    tmt_tests = result['tmt_tests']
    # The info plan has no results: the stage itself has failed
    assert tmt_tests['success'] is False
    assert tmt_tests['tmt_exit_code'] == 143
    assert tmt_tests['plans'] == [
        {'name': '/plans/e2e/gating', 'counts': {'pass': 1, 'pending': 1}},
        {'name': '/plans/e2e/info', 'error': 'no results'},
    ]
    assert result['stop_environment'] == {'success': True}
    stage_log = next(
        name for name in FakeUploader.uploaded
        if name.startswith('tmt_tests_')
    )
    assert stage_log == f'tmt_tests_{flow.task_id}.log'


def test_discover_failed(flow: Flow, tmp_path: Path):
    """
    The discover step of a plan fails (the repository of a discover
    phase can't be cloned): tmt exits with 2 and leaves an empty
    results.yaml for the plan. The stage fails, ALBS gets
    tmt_tests.success false, the stage log says which plan and why.
    """
    error = (
        "Command 'git clone https://git.example.org/tests/other.git "
        "/var/tmp/tmt/run/plans/e2e/info/discover/other/tests' returned 128."
    )
    scenario = scenario_copy(
        tmp_path, 'e2e-plans', discover_fail={'/plans/e2e/info': error},
    )
    result = flow.run(scenario, load_payload('e2e-plans'))

    tmt_tests = result['tmt_tests']
    assert tmt_tests['success'] is False
    assert tmt_tests['tmt_exit_code'] == 2
    gating, info = tmt_tests['plans']
    assert gating['name'] == '/plans/e2e/gating'
    assert 'counts' in gating
    assert info == {
        'name': '/plans/e2e/info',
        'error': f'discover failed: fail: {error}',
    }
    # The tests of the plan that has run are still reported
    assert result['tmt']['tmt_test_/e2e/pass']['result'] == 'pass'
    assert result['stop_environment'] == {'success': True}
    (container,) = flow.containers().values()
    assert container['destroyed']


def test_unknown_commit(flow: Flow):
    """A well-formed commit tests/<pkg> doesn't have: no fallback."""
    payload = load_payload('grep-al9')
    payload['package_tests']['commit'] = 'f' * 40
    result = flow.run(FIXTURES / 'grep-al9', payload)
    assert result['tmt_tests'] == {
        'success': False,
        'url': 'https://git.almalinux.org/tests/grep.git',
        'commit': 'f' * 40,
    }
    assert 'tmt' not in result
    assert flow.calls('tmt') == []
    fetches = [
        call['args'] for call in flow.calls('git') if call['args'][0] == 'fetch'
    ]
    assert fetches == [
        ['fetch', '-q', '--depth', '1', 'origin', 'f' * 40],
        ['fetch', '-q', 'origin'],
    ]


def test_short_commit_is_refused(flow: Flow):
    """ALTS refuses a payload with an abbreviated commit, others go on."""
    bad = load_payload('grep-al9')
    bad['package_tests']['commit'] = bad['package_tests']['commit'][:7]
    good = load_payload('grep-al9')
    good['bs_task_id'] = 1003
    scheduled = flow.schedule([bad, good])
    assert [args[0]['bs_task_id'] for args, _ in scheduled] == [1003]
    with pytest.raises(ValueError, match='full git commit hash'):
        TaskRequestPayload(**bad)


@pytest.mark.parametrize(
    'dist_version, dist_arch, image, platform',
    [
        pytest.param(
            '9', 'x86_64', 'almalinux/9-init:9', 'linux/amd64',
            id='almalinux-9',
        ),
        pytest.param(
            '8', 'i686', 'almalinux/8-init:8', 'linux/386',
            id='almalinux-8-i686',
        ),
        pytest.param(
            '10-kitten', 'x86_64_v2', 'almalinux/10-kitten-init:10-kitten',
            'linux/amd64/v2', id='kitten-x86_64_v2',
        ),
    ],
)
def test_package_task_takes_the_old_path(
    flow: Flow,
    dist_version: str,
    dist_arch: str,
    image: str,
    platform: str,
):
    """
    A package test task without package_tests runs the package stages
    as before, in the init image of docker_images with the init flags
    like a tmt task (one container kind for all the docker tasks).
    """
    payload = load_payload('grep-al9')
    del payload['package_tests']
    payload['runner_type'] = 'any'
    payload['dist_version'] = dist_version
    payload['dist_arch'] = dist_arch
    flow.monkeypatch.setattr(
        scheduling.random, 'choice', lambda choices: 'docker',
    )
    result = flow.run(FIXTURES / 'grep-al9', payload)

    (container,) = flow.containers().values()
    assert_init_container(container, image)
    assert container['platform'] == platform
    assert container['image_platform'] == platform
    # The worker has only the x86_64 image of the tag (fake_tools):
    # the task pulls its own platform and the container runs it
    assert container['runtime_platform'] == platform
    pulls = [call['args'] for call in flow.calls('docker')
             if call['args'][0] == 'pull']
    assert pulls == (
        [] if platform == 'linux/amd64'
        else [['pull', '--platform', platform, image]]
    )
    variables = flow.provision_variables()
    assert "'tmt_is_needed': False" in variables
    assert "'init_container': True" in variables
    assert "'pytest_is_needed': True" in variables
    tags = [call['tags'] for call in flow.calls('ansible-playbook')]
    assert tags == ['initial_provision', 'install_package', 'uninstall_package']
    assert flow.calls('git') == []
    assert flow.calls('tmt') == []
    for stage in ('install_package', 'third_party_tests_wrapper',
                  'uninstall_package'):
        assert result.get(stage) == {'success': True}, stage
    assert result['tests']['package_integrity_tests']['success'] is True
    assert 'tmt_tests' not in result
    assert 'tmt' not in result
    assert result['skipped_tests'] == []
