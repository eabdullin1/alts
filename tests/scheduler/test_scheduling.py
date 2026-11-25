import pytest
from pydantic import ValidationError

from alts.scheduler import CONFIG
from alts.scheduler import scheduling
from alts.shared.models import QueueArchOverride, TaskRequestPayload
from alts.worker.runners import DockerRunner

get_queue_arch = scheduling.TestsScheduler.get_queue_arch


def make_payload(dist_name: str, dist_version, dist_arch: str):
    return TaskRequestPayload(
        bs_task_id=1,
        runner_type='docker',
        dist_name=dist_name,
        dist_version=dist_version,
        dist_arch=dist_arch,
        package_name='bash',
    )


class TestGetQueueArch:
    @pytest.mark.parametrize(
        'dist_name, dist_version, dist_arch, expected',
        [
            pytest.param(
                'almalinux', '10', 'x86_64', 'x86_64_v3',
                id='major-version',
            ),
            pytest.param(
                'almalinux', '10.1', 'x86_64', 'x86_64_v3',
                id='full-version',
            ),
            pytest.param(
                'almalinux', 10, 'x86_64', 'x86_64_v3',
                id='int-version',
            ),
            pytest.param(
                'almalinux', '10', 'i686', 'x86_64_v3',
                id='mapped-arch',
            ),
            pytest.param(
                'almalinux', '10', 'x86_64_v2', 'x86_64_v2',
                id='other-arch',
            ),
            pytest.param(
                'almalinux', '9', 'x86_64', 'x86_64',
                id='other-version',
            ),
            pytest.param(
                'centos', '10', 'x86_64', 'x86_64',
                id='other-dist',
            ),
            pytest.param(
                'almalinux', '10', 'aarch64', 'aarch64',
                id='no-override-for-arch',
            ),
        ],
    )
    def test_get_queue_arch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dist_name: str,
        dist_version,
        dist_arch: str,
        expected: str,
    ):
        monkeypatch.setattr(
            CONFIG,
            'queue_arch_overrides',
            [
                QueueArchOverride(
                    dist_name='almalinux',
                    dist_versions=['10'],
                    arch='x86_64',
                    queue_arch='x86_64_v3',
                ),
            ],
        )
        payload = make_payload(dist_name, dist_version, dist_arch)
        assert get_queue_arch(DockerRunner, payload) == expected

    def test_no_overrides(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(CONFIG, 'queue_arch_overrides', [])
        payload = make_payload('almalinux', '10', 'x86_64')
        assert get_queue_arch(DockerRunner, payload) == 'x86_64'

    def test_unknown_arch(self):
        payload = make_payload('almalinux', '10', 'riscv64')
        assert get_queue_arch(DockerRunner, payload) is None

    def test_undeclared_queue_arch_rejected(self):
        with pytest.raises(ValidationError):
            QueueArchOverride(
                dist_name='almalinux',
                dist_versions=['10'],
                arch='x86_64',
                queue_arch='x86-64-v3',
            )


class FakeSession:
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


def make_scheduler() -> scheduling.TestsScheduler:
    import threading

    return scheduling.TestsScheduler(
        threading.Event(),
        threading.Event(),
        celery_app=None,
    )


def test_tmt_task_goes_to_docker(monkeypatch: pytest.MonkeyPatch):
    queues = []
    monkeypatch.setattr(CONFIG, 'supported_runners', 'all')
    monkeypatch.setattr(CONFIG, 'queue_arch_overrides', [])
    monkeypatch.setattr(scheduling, 'Session', FakeSession)
    monkeypatch.setattr(
        scheduling.run_tests,
        'apply_async',
        lambda *args, **kwargs: queues.append(kwargs['queue']),
    )
    # Without tmt the runner is random, with tmt it's always docker
    monkeypatch.setattr(
        scheduling.random,
        'choice',
        lambda choices: 'opennebula',
    )
    scheduler = make_scheduler()
    payload = TaskRequestPayload(
        bs_task_id=1,
        runner_type='any',
        dist_name='almalinux',
        dist_version='9',
        dist_arch='x86_64',
        package_name='chrony',
        package_tests={
            'url': 'https://git.almalinux.org/tests/chrony.git',
            'commit': 'a' * 40,
            'tmt': {'plans': ['/plans/tier1/public']},
        },
    )
    scheduler.schedule_test_task(payload)
    assert queues == ['docker-x86_64-0']


def test_invalid_payload_does_not_stop_scheduler(
    monkeypatch: pytest.MonkeyPatch,
):
    scheduler = make_scheduler()
    good = {
        'bs_task_id': 2,
        'runner_type': 'docker',
        'dist_name': 'almalinux',
        'dist_version': '9',
        'dist_arch': 'x86_64',
        'package_name': 'chrony',
    }
    bad = dict(good, bs_task_id=1, package_tests={'url': 'x', 'commit': 'main'})
    scheduled = []
    monkeypatch.setattr(
        scheduler,
        'get_available_test_tasks',
        lambda: [bad, good],
    )
    monkeypatch.setattr(
        scheduler,
        'schedule_test_task',
        lambda payload: scheduled.append(payload.bs_task_id),
    )

    def stop(_):
        scheduler._TestsScheduler__graceful_terminate.set()
        scheduler._TestsScheduler__terminated_event.set()

    monkeypatch.setattr(scheduling.time, 'sleep', stop)
    scheduler.run()
    assert scheduled == [2]
