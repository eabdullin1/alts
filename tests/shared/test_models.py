from typing import List, Optional

import pytest

from pydantic import ValidationError

from alts.shared.models import TaskRequestPayload


@pytest.mark.parametrize(
    'skipped_tests',
    [
        pytest.param(None, id='nothing-skipped'),
        pytest.param(
            ['uninstall_package', 'third_party_test_*.bats'],
            id='skipped-tests',
        ),
    ],
)
def test_task_request_payload_skipped_tests(
    skipped_tests: Optional[List[str]],
):
    # The build system sends null for the tasks that skip nothing
    payload = TaskRequestPayload(
        bs_task_id=1,
        runner_type='docker',
        dist_name='almalinux',
        dist_version='9',
        dist_arch='x86_64',
        package_name='foo',
        skipped_tests=skipped_tests,
    )
    assert payload.model_dump()['skipped_tests'] == skipped_tests


def make_payload(**kwargs) -> TaskRequestPayload:
    return TaskRequestPayload(
        bs_task_id=1,
        runner_type='docker',
        dist_name='almalinux',
        dist_version='9',
        dist_arch='x86_64',
        package_name='chrony',
        **kwargs,
    )


def test_task_request_payload_package_tests():
    package_tests = {
        'url': 'https://git.almalinux.org/tests/chrony.git',
        'commit': 'A' * 40,
        'packages': ['chrony-4.6-1.el9.x86_64'],
        'tmt': {
            'plans': ['/plans/tier1/public'],
            'filter': 'tier: 1',
            'context': {'component': 'chrony'},
            'environment': {'TEST_PROFILE': 'container'},
            'unknown': 'ignored',
        },
    }
    payload = make_payload(package_tests=package_tests)
    assert payload.model_dump()['package_tests'] == {
        'url': 'https://git.almalinux.org/tests/chrony.git',
        'commit': 'a' * 40,
        'packages': ['chrony-4.6-1.el9.x86_64'],
        'tmt': {
            'plans': ['/plans/tier1/public'],
            'filter': 'tier: 1',
            'context': {'component': 'chrony'},
            'environment': {'TEST_PROFILE': 'container'},
        },
    }
    assert make_payload().model_dump()['package_tests'] is None


@pytest.mark.parametrize(
    'gating',
    [
        pytest.param({}, id='without-gating'),
        pytest.param(
            {'gating': ['/plans/tier1/public']},
            id='old-gating-list',
        ),
        pytest.param({'gating': True}, id='gating-flag'),
    ],
)
def test_package_tests_gating_is_ignored(gating: dict):
    """
    ALTS has no gating: every plan of the request is run. A request
    without it works, and a gating field, in tmt or in package_tests,
    is accepted and dropped.
    """
    package_tests = {
        'url': 'https://git.almalinux.org/tests/cups.git',
        'commit': 'b' * 40,
        'tmt': {
            'plans': ['/plans/tier1/public', '/plans/others/public'],
            **gating,
        },
        **gating,
    }
    dumped = make_payload(package_tests=package_tests).model_dump()
    assert dumped['package_tests'] == {
        'url': 'https://git.almalinux.org/tests/cups.git',
        'commit': 'b' * 40,
        'packages': [],
        'tmt': {
            'plans': ['/plans/tier1/public', '/plans/others/public'],
            'filter': None,
            'context': {},
            'environment': {},
        },
    }


@pytest.mark.parametrize('commit', ['main', 'a' * 7, 'g' * 40, ''])
def test_package_tests_needs_full_commit(commit: str):
    with pytest.raises(ValidationError, match='full git commit hash'):
        make_payload(package_tests={'url': 'https://x/y.git', 'commit': commit})
