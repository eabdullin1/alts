"""
Provision of the tmt tasks in the preparation role (ALTS-6): what the
init container gets besides tmt itself.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from alts.worker import RESOURCES_DIR

ROLE_DIR = Path(RESOURCES_DIR, 'roles', 'preparation')


def load_yaml(*parts: str):
    return yaml.safe_load(Path(ROLE_DIR, *parts).read_text())


def get_task(name: str) -> dict:
    tasks = load_yaml('tasks', 'rhel.yml')
    return next(task for task in tasks if task.get('name') == name)


def test_tmt_extra_packages():
    defaults = load_yaml('defaults', 'main.yml')
    # bind tests need logrotate, openssl and man-db
    assert {'logrotate', 'openssl', 'man-db'} <= set(
        defaults['tmt_extra_packages']
    )
    task = get_task(
        'Install packages tests expect from CI machines (tmt tasks)'
    )
    # A package missing on a platform doesn't fail the provision
    assert '--setopt=strict=0' in task['command']
    assert 'tmt_extra_packages' in task['command']
    assert task['when'] == 'tmt_is_needed | bool'


@pytest.mark.parametrize(
    'content, expected',
    [
        pytest.param(None, 'comment', id='missing'),
        pytest.param('', 'comment', id='empty'),
        pytest.param(
            'UUID=1 / xfs defaults 0 0\n',
            'UUID=1 / xfs defaults 0 0\n',
            id='kept',
        ),
    ],
)
def test_fstab_is_not_empty(tmp_path: Path, content, expected: str):
    """
    cups tests print /etc/fstab with lp, which refuses an empty file:
    the provision writes a comment line into a missing or empty fstab
    and keeps a non-empty one.
    """
    comment = load_yaml('defaults', 'main.yml')['tmt_fstab_comment']
    assert comment.startswith('#')
    task = get_task('Make /etc/fstab not empty (tmt tasks)')
    assert task['when'] == 'tmt_is_needed | bool'
    fstab = Path(tmp_path, 'fstab')
    if content is not None:
        fstab.write_text(content)
    script = (
        task['shell']
        .replace('/etc/fstab', str(fstab))
        .replace('{{ tmt_fstab_comment }}', comment)
    )
    assert '{{' not in script
    stdout = subprocess.run(
        ['sh', '-c', script],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if expected == 'comment':
        assert fstab.read_text() == comment + '\n'
        assert 'created' in stdout
    else:
        assert fstab.read_text() == expected
        assert stdout == ''
