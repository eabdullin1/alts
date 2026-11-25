import shutil
from logging import Logger
from pathlib import Path
from random import randint
from time import sleep
from typing import Optional, Tuple

from plumbum import local

from alts.shared.utils.path_utils import get_abspath


def checkout(
    git_ref: str,
    git_repo_path: Path,
    logger: Logger,
    cmd_timeout: int = 300,
):
    logger.debug('Switching to the git branch/tag: %s', git_ref)
    exit_code, _, stderr = local['bash'].with_cwd(git_repo_path).run(
        ['-c', f'git fetch origin && git checkout {git_ref}'],
        retcode=None,
        timeout=cmd_timeout,
    )


def git_reset_hard(git_repo_path: Path, logger: Logger):
    exit_code, _, stderr = local['bash'].with_cwd(git_repo_path).run(
        ['-c', 'git checkout master && git reset --hard origin/master'],
        retcode=None,
    )
    if exit_code != 0:
        logger.error(
            'Cannot reset the git index and working tree:\n%s',
            stderr,
        )


def __clone_git_repo(
    repo_url: str,
    work_dir: Path,
    logger: Logger,
    reference_directory: Optional[str] = None,
    cmd_timeout: int = 300,
):
    git_repo_path = Path(
        work_dir,
        Path(repo_url).name.replace('.git', ''),
    )
    if git_repo_path.exists():
        return git_repo_path
    logger.debug('Cloning the git repo: %s', repo_url)
    args = ['clone', repo_url, '--depth', '1']
    if reference_directory:
        args.extend(
            ['--reference-if-able', get_abspath(reference_directory)]
        )
    exit_code, stdout, stderr = local['git'].with_cwd(work_dir).run(
        args,
        retcode=None,
        timeout=cmd_timeout,
    )
    if exit_code == 0:
        return git_repo_path
    logger.error(
        'Unable to clone the git repo %s:\n%s\n\n%s',
        repo_url, stdout, stderr,
    )
    return


def clone_git_repo(
    repo_url: str,
    git_ref: str,
    work_dir: Path,
    logger: Logger,
    reference_directory: Optional[str] = None,
    cmd_timeout: int = 300,
) -> Optional[Path]:
    git_repo_path = __clone_git_repo(
        repo_url,
        work_dir,
        logger,
        reference_directory=reference_directory,
        cmd_timeout=cmd_timeout,
    )
    if git_repo_path:
        checkout(git_ref, git_repo_path, logger)
    return git_repo_path


def prepare_gerrit_command(git_ref: str) -> str:
    command = ''
    if git_ref == 'master':
        command = 'git checkout master && git pull'
    elif '/' not in git_ref and not git_ref.isdigit():
        command = (
            f'git reset --hard origin/{git_ref} && '
            f'git checkout {git_ref} && git pull'
        )
    elif all(git_ref.split('/')):
        review, patchset = git_ref.split('/')
        sm = review[-2:]
        command = (
            'git checkout master && git pull && '
            f"git fetch origin 'refs/changes/{sm}/{review}/{patchset}' "
            '--force --update-head-ok --progress && '
            'git checkout FETCH_HEAD'
        )
    return command


def clone_gerrit_repo(
    repo_url: str,
    git_ref: str,
    work_dir: Path,
    logger: Logger,
    reference_directory: Optional[str] = None,
    cmd_timeout: int = 300,
) -> Optional[Path]:
    # ssh://gerrit.test.com:00000/repo
    git_repo_path = __clone_git_repo(
        repo_url,
        work_dir,
        logger,
        reference_directory=reference_directory,
        cmd_timeout=cmd_timeout,
    )
    if git_repo_path:
        gerrit_command = prepare_gerrit_command(git_ref)
        if not gerrit_command:
            logger.debug('Nothing to do, skipping')
            return
        exit_code, _, stderr = local['bash'].with_cwd(git_repo_path).run(
            ['-c', gerrit_command],
            retcode=None,
            timeout=cmd_timeout,
        )
        if exit_code != 0:
            logger.error(
                'Cannot execute gerrit command: %s\n%s',
                gerrit_command,
                stderr,
            )
            return
    return git_repo_path


def fetch_git_commit(
    repo_url: str,
    commit: str,
    work_dir: Path,
    logger: Logger,
    cmd_timeout: int = 300,
) -> Tuple[Optional[Path], str]:
    """
    Gets exactly the given commit of the repository into
    work_dir/<repository name> and checks it out.

    "git clone --depth 1" can't get an arbitrary commit, so the commit is
    fetched by its hash, and if the server doesn't allow it, all the
    branches are fetched. There is no fallback to another commit: tests
    of another commit would be reported as the tests of this one.

    Returns the repository path, or None and the error.
    """
    git_repo_path = Path(work_dir, Path(repo_url).name)
    if git_repo_path.name.endswith('.git'):
        git_repo_path = git_repo_path.with_name(git_repo_path.name[:-4])
    if git_repo_path.exists():
        shutil.rmtree(git_repo_path)
    git_repo_path.mkdir(parents=True)
    git = local['git'].with_cwd(git_repo_path)

    def run(*args) -> Tuple[int, str, str]:
        return git.run(args, retcode=None, timeout=cmd_timeout)

    errors = []
    for args in (('init', '-q'), ('remote', 'add', 'origin', repo_url)):
        exit_code, _, stderr = run(*args)
        if exit_code != 0:
            return None, f'git {" ".join(args)} failed:\n{stderr}'
    logger.debug('Fetching %s of %s', commit, repo_url)
    exit_code, _, stderr = run('fetch', '-q', '--depth', '1', 'origin', commit)
    if exit_code != 0:
        errors.append(stderr)
        logger.debug('Cannot fetch the commit by hash, fetching branches')
        exit_code, _, stderr = run('fetch', '-q', 'origin')
        if exit_code != 0:
            errors.append(stderr)
            return None, (
                f'Cannot fetch {repo_url}:\n' + '\n'.join(errors)
            )
    exit_code, _, stderr = run('checkout', '-q', '--detach', commit)
    if exit_code != 0:
        errors.append(stderr)
        return None, (
            f'Commit {commit} is not in {repo_url}:\n' + '\n'.join(errors)
        )
    exit_code, stdout, stderr = run('rev-parse', 'HEAD')
    head = stdout.strip()
    if exit_code != 0 or head != commit:
        return None, f'Checked out {head or stderr} instead of {commit}'
    return git_repo_path, ''
