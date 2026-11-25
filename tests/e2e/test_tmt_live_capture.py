"""
The whole ALTS task against the data of a real ALBS -> ALTS -> ALBS run.

tests/fixtures/tmt/live has what went over the wire on the test stand
(a logging proxy between ALTS and ALBS) and what tmt wrote there: the
payloads ALBS returned from get_test_tasks, results.yaml and output.txt
of every plan, the tmt_tests stage log and the result ALTS posted back.
ALTS gets the very same payloads here, runs its real code with the fake
tools of tests/e2e/fake_tools.py, which replay that tmt run, and must
post the very same result. See tests/fixtures/tmt/live/README.md.
"""

import gzip
import json
import os
import shlex
import uuid
from pathlib import Path
from typing import Dict, List

import pytest

from alts.scheduler import scheduling
from alts.worker import tasks
from alts.worker.runners import base as base_runner
from tests.e2e.test_tmt_task_flow import (  # noqa: F401 (fixture)
    FakeUploader,
    Flow,
    assert_init_container,
    flow,
)

LIVE = Path(__file__).parents[1] / 'fixtures' / 'tmt' / 'live'


def live(scenario: str, name: str):
    path = Path(LIVE, scenario, name)
    if path.suffix == '.json':
        return json.loads(path.read_text())
    return path.read_text()


def component_payload(scenario: str) -> dict:
    (payload,) = [
        payload
        for payload in live(scenario, 'albs_payloads.json')
        if payload['package_tests']
    ]
    return payload


class ReadingUploader(FakeUploader):
    """Keeps the text of the logs ALTS uploads, they are gzipped files."""

    logs: Dict[str, str] = {}

    def upload(self, artifacts_dir: str, upload_dir: str, **kwargs):
        for name in os.listdir(artifacts_dir):
            data = Path(artifacts_dir, name).read_bytes()
            ReadingUploader.logs[name] = gzip.decompress(data).decode()
        return super().upload(artifacts_dir, upload_dir, **kwargs)


def stage_log_head(text: str) -> List[str]:
    """What ALTS itself writes into the tmt_tests log, before tmt output."""
    lines = text.split('Stdout:', 1)[1].split('tmt output:', 1)[0]
    return [line for line in lines.splitlines() if line.strip()]


def run_live(flow: Flow, scenario: str) -> dict:
    """Runs the component task of the scenario with its real task ID."""
    task_id = live(scenario, 'task_id').strip()
    flow.monkeypatch.setattr(
        scheduling.uuid,
        'uuid4',
        lambda: uuid.UUID(task_id),
    )
    ReadingUploader.logs = {}
    flow.monkeypatch.setattr(base_runner, 'PulpLogsUploader', ReadingUploader)
    result = flow.run(Path(LIVE, scenario), component_payload(scenario))
    assert flow.task_id == task_id
    return result


def assert_same_result(flow: Flow, result: dict, scenario: str):
    """The result is what ALTS posted on the stand, the logs too."""
    expected = live(scenario, 'alts_result.json')
    assert flow.callback['json']['api_version'] == expected['api_version']
    assert {k: v for k, v in result.items() if k != 'logs'} == {
        k: v for k, v in expected['result'].items() if k != 'logs'
    }
    # Pulp gives other hrefs here, the log files are the same
    assert sorted(log['name'] for log in result['logs']) == sorted(
        log['name'] for log in expected['result']['logs']
    )
    assert sorted(flow.callback['json']['stats']) == sorted(expected['stats'])


def assert_same_stage_log(flow: Flow, scenario: str):
    (name,) = [
        name for name in ReadingUploader.logs if name.startswith('tmt_tests_')
    ]
    assert name == f'tmt_tests_{flow.task_id}.log'
    assert stage_log_head(ReadingUploader.logs[name]) == stage_log_head(
        live_stage_log(scenario)
    )


def live_stage_log(scenario: str) -> str:
    """
    The captured stage log as ALTS writes it now: the live run (24
    November) predates "-e HOME=/root", which ALTS has passed to tmt
    since cups-filters needed it (foomatic-rip crashes without HOME).
    """
    return live(scenario, 'tmt_tests_stage.txt').replace(
        ' -e PATH=', ' -e HOME=/root -e PATH=',
    )


def real_tmt_command(scenario: str) -> List[str]:
    (line,) = [
        line
        for line in live_stage_log(scenario).splitlines()
        if line.startswith('Command: ')
    ]
    return shlex.split(line[len('Command: ') :])


def tmt_exec_commands(flow: Flow, discover_only: bool = False) -> List[list]:
    commands = []
    for call in flow.calls('docker'):
        args = call['args']
        if args[0] != 'exec' or 'tmt' not in args or 'run' not in args:
            continue
        command = args[args.index('timeout') :] if 'timeout' in args else []
        if ('--all' in args) is not discover_only:
            commands.append(command)
    return commands


@pytest.mark.parametrize('scenario', ['grep-al9', 'e2e-plans'])
def test_live_payloads_are_scheduled(flow: Flow, scenario: str):
    """ALBS payloads as they came over the wire pass the scheduler."""
    payloads = live(scenario, 'albs_payloads.json')
    scheduled = flow.schedule(payloads)
    assert len(scheduled) == len(payloads)
    by_task = {
        args[0]['bs_task_id']: (args[0], kwargs) for args, kwargs in scheduled
    }
    for payload in payloads:
        task_params, kwargs = by_task[payload['bs_task_id']]
        assert kwargs['queue'] == 'docker-x86_64-0'
        assert task_params['package_name'] == payload['package_name']
        assert task_params['skipped_tests'] == payload['skipped_tests']
        if not payload['package_tests']:
            assert task_params['package_tests'] is None
            continue
        # The whole package_tests reaches the worker; ALTS adds no plans
        package_tests = payload['package_tests']
        assert task_params['package_tests'] == dict(
            package_tests,
            tmt=dict(package_tests['tmt'], filter=None),
        )


def test_live_grep_component_task(flow: Flow):
    """tests/grep on AlmaLinux 9: 25 pass, 2 fail in tier2-tier3."""
    result = run_live(flow, 'grep-al9')
    (container,) = flow.containers().values()
    assert_init_container(container, 'almalinux/9-init:9')
    # The tmt command is the one ALTS ran on the stand
    assert tmt_exec_commands(flow) == [real_tmt_command('grep-al9')]
    assert tmt_exec_commands(flow, discover_only=True) == []
    assert_same_result(flow, result, 'grep-al9')
    assert_same_stage_log(flow, 'grep-al9')


def test_live_skipped_tmt_test(flow: Flow):
    """tmt_test_/e2e/skipme of skipped_tests is left out of the tmt run.

    It's in the gating plan and fails if it runs (extra_tests of the
    scenario), the live result doesn't have it.
    """
    payload = component_payload('e2e-plans')
    assert payload['skipped_tests'] == ['tmt_test_/e2e/skipme']
    result = run_live(flow, 'e2e-plans')
    (command,) = tmt_exec_commands(flow)
    assert command == real_tmt_command('e2e-plans')
    assert command[-3:] == ['tests', '--name', '^(?!(?:/e2e/skipme)$)']
    # The fake tmt saw the test and the regex excluded it
    (tmt_run,) = flow.tmt_runs()
    assert tmt_run['executed'] == {
        '/plans/e2e/gating': ['/e2e/pass'],
        '/plans/e2e/info': ['/e2e/fail'],
    }
    assert 'tmt_test_/e2e/skipme' not in result['tmt']
    assert result['skipped_tests'] == ['tmt_test_/e2e/skipme']
    # A failed test fails neither the stage nor the task in ALTS: that is
    # up to ALBS
    assert result['tmt']['tmt_test_/e2e/fail']['success'] is False
    assert result['tmt']['tmt_test_/e2e/fail']['plan'] == '/plans/e2e/info'
    assert result['tmt_tests']['success'] is True
    assert_same_result(flow, result, 'e2e-plans')
    assert_same_stage_log(flow, 'e2e-plans')


def test_live_skipped_tmt_test_would_run(flow: Flow):
    """Without skipped_tests the same payload runs /e2e/skipme."""
    payload = component_payload('e2e-plans')
    payload['skipped_tests'] = None
    flow.run(Path(LIVE, 'e2e-plans'), payload)
    (tmt_run,) = flow.tmt_runs()
    assert tmt_run['executed']['/plans/e2e/gating'] == [
        '/e2e/pass',
        '/e2e/skipme',
    ]


def test_live_package_task_takes_the_old_path(flow: Flow):
    """A real package payload of the same build: no tmt, the package
    stages in the same init image as the component task."""
    (payload, *_) = [
        payload
        for payload in live('grep-al9', 'albs_payloads.json')
        if not payload['package_tests']
    ]
    result = flow.run(Path(LIVE, 'grep-al9'), payload)
    (container,) = flow.containers().values()
    assert_init_container(container, 'almalinux/9-init:9')
    assert flow.calls('tmt') == []
    assert flow.calls('git') == []
    assert 'tmt_tests' not in result and 'tmt' not in result
    assert result['install_package'] == {'success': True}


def test_live_tmt_exit_code_is_not_a_stage_failure(flow: Flow):
    """tmt exits with 1 when a test fails, the stage still succeeds.

    The stage only fails when tmt hasn't run the plans; which failed
    tests matter is up to ALBS.
    """
    result = run_live(flow, 'grep-al9')
    (tmt_run,) = flow.tmt_runs()
    assert tmt_run['executed']
    assert result['tmt_tests']['tmt_exit_code'] == 1
    assert result['tmt_tests']['success'] is True
    assert result['tmt_tests']['counts'] == {'pass': 25, 'fail': 2}


def test_live_component_task_cancelled(flow: Flow):
    """ALBS cancels the component task after ALTS took it.

    ALTS aborts the task (cancel_tasks of the scheduler): the next stage
    doesn't start, the environment is destroyed, ALBS gets revoked.
    """

    def is_aborted(*args, **kwargs):
        # Cancelled while the environment was being provisioned
        return any(
            call['tags'] == 'initial_provision'
            for call in flow.calls('ansible-playbook')
        )

    flow.monkeypatch.setattr(
        tasks.run_tests, 'is_aborted', is_aborted, raising=False
    )
    payload = component_payload('grep-al9')
    result = flow.run(Path(LIVE, 'grep-al9'), payload)
    assert result['revoked'] is True
    assert 'tmt_tests' not in result and 'tmt' not in result
    assert flow.calls('tmt') == []
    (container,) = flow.containers().values()
    assert container['destroyed']
    assert flow.callback['url'].endswith(payload['callback_href'])
