import logging
import subprocess
from pathlib import Path

import pytest

from alts.shared.utils.git_utils import fetch_git_commit

LOGGER = logging.getLogger(__name__)


def git(*args, cwd: Path) -> str:
    return subprocess.run(
        ['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    """A repository with two commits on main, returns (url, commits)."""
    path = Path(tmp_path, 'origin', 'chrony')
    path.mkdir(parents=True)
    git('init', '-q', '-b', 'main', cwd=path)
    commits = []
    for content in ('first', 'second'):
        Path(path, 'file').write_text(content)
        git('add', 'file', cwd=path)
        git('commit', '-q', '-m', content, cwd=path)
        commits.append(git('rev-parse', 'HEAD', cwd=path))
    return f'file://{path}', commits


def test_fetch_git_commit(tmp_path: Path, repo):
    url, commits = repo
    work_dir = Path(tmp_path, 'work')
    # Not the head of the branch: depth 1 clone would not have it
    path, error = fetch_git_commit(url, commits[0], work_dir, LOGGER)
    assert error == ''
    assert path == Path(work_dir, 'chrony')
    assert Path(path, 'file').read_text() == 'first'
    # Fetching again replaces the previous checkout
    path, error = fetch_git_commit(url, commits[1], work_dir, LOGGER)
    assert Path(path, 'file').read_text() == 'second'


def test_fetch_missing_commit(tmp_path: Path, repo):
    url, _ = repo
    path, error = fetch_git_commit(url, 'b' * 40, Path(tmp_path, 'w'), LOGGER)
    assert path is None
    assert f'Commit {"b" * 40} is not in {url}' in error
