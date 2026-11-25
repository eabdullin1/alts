import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from alts.shared.exceptions import TmtTestsError
from alts.worker import CONFIG
from alts.worker.runners import DockerRunner
from alts.worker.runners import base as base_runner
from alts.worker.runners import docker as docker_runner

COMMIT = 'a' * 40
PACKAGE_TESTS = {
    'url': 'https://git.almalinux.org/tests/chrony.git',
    'commit': COMMIT,
    'tmt': {
        'plans': ['/plans/tier1/public', '/plans/tier1/internal'],
        'context': {'component': 'chrony'},
    },
}
RESULTS_YAML = """
- name: /Sanity/basic
  result: pass
  duration: '00:00:05'
  log:
    - data/guest/default-0/Sanity/basic-1/output.txt
- name: /Sanity/broken
  result: fail
  note: [failed in the test phase]
  log:
    - data/guest/default-0/Sanity/broken-1/output.txt
- name: /Sanity/warned
  result: warn
- name: /Sanity/upstream-skip
  result: skip
"""


def task_is_aborted():
    return False


@pytest.fixture(autouse=True)
def no_env_cleanup(monkeypatch: pytest.MonkeyPatch):
    # BaseRunner.__del__ destroys the environment of a runner that has
    # a working directory
    monkeypatch.setattr(base_runner.BaseRunner, '__del__', lambda self: None)


def make_runner(
    dist_version: str = '9',
    package_tests: Optional[dict] = None,
    dist_name: str = 'almalinux',
    work_dir: Optional[Path] = None,
    dist_arch: str = 'x86_64',
) -> DockerRunner:
    runner = DockerRunner(
        'task_id',
        task_is_aborted,
        dist_name,
        dist_version,
        dist_arch=dist_arch,
        package_tests=package_tests,
    )
    if work_dir:
        runner._work_dir = str(work_dir)
    return runner


class TestInitContainer:
    @pytest.mark.parametrize(
        'package_tests',
        [
            pytest.param(PACKAGE_TESTS, id='tmt-task'),
            pytest.param(None, id='package-task'),
            pytest.param(
                {'url': 'https://x/y.git', 'commit': COMMIT},
                id='package-tests-without-tmt',
            ),
        ],
    )
    @pytest.mark.parametrize(
        'dist_version, dist_arch, image, platform',
        [
            pytest.param(
                '9', 'x86_64', 'almalinux/9-init:9', 'linux/amd64', id='9',
            ),
            pytest.param(
                '9', 'i686', 'almalinux/9-init:9', 'linux/386', id='9-i686',
            ),
            pytest.param(
                '8.10', 'aarch64', 'almalinux/8-init:8', 'linux/arm64/v8',
                id='8.10',
            ),
            pytest.param(
                '10-kitten', 'x86_64_v2', 'almalinux/10-kitten-init:10-kitten',
                'linux/amd64/v2', id='kitten-x86_64_v2',
            ),
        ],
    )
    def test_render_init_container(
        self,
        tmp_path: Path,
        dist_version: str,
        dist_arch: str,
        image: str,
        platform: str,
        package_tests: Optional[dict],
    ):
        """
        Every docker task, tmt or not, runs in the init image of
        docker_images (tests/tests_config.yaml) with the init flags. The
        container asks for the platform of the task: x86_64 and i686
        tasks share a multi-platform tag.
        """
        runner = DockerRunner(
            'task_id',
            task_is_aborted,
            'almalinux',
            dist_version,
            dist_arch=dist_arch,
            package_tests=package_tests,
        )
        runner._work_dir = str(tmp_path)
        assert runner.init_container
        runner._render_tf_main_file()
        tf = Path(tmp_path, 'docker.tf').read_text()
        assert f'name         = "{image}"' in tf
        container, image_block = tf.split('resource "docker_image"')
        assert f'  platform = "{platform}"' in container
        assert f'platform     = "{platform}"' in image_block
        assert 'cgroupns_mode = "private"' in tf
        assert 'security_opts = ["writable-cgroups=true"]' in tf
        assert re.search(r'tmpfs = \{\s*"/run" = ""\s*"/run/lock" = ""\s*\}', tf)
        assert 'command' not in tf
        assert 'privileged =' not in tf

    @pytest.mark.parametrize(
        'cgroup_v2, docker_version, error',
        [
            pytest.param(True, (0, '29.1.0'), None, id='supported'),
            pytest.param(True, (0, '28.0.0'), None, id='minimal-docker'),
            pytest.param(
                False, (0, '29.1.0'), 'need cgroup v2', id='cgroup-v1',
            ),
            pytest.param(
                True, (0, '27.5.1'), r'need Docker >= 28\.0', id='old-docker',
            ),
            pytest.param(
                True, (1, 'Cannot connect'), 'Cannot get the Docker',
                id='no-docker',
            ),
        ],
    )
    def test_check_init_container_support(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cgroup_v2: bool,
        docker_version: Tuple[int, str],
        error: Optional[str],
    ):
        monkeypatch.setattr(
            docker_runner.os.path,
            'exists',
            lambda path: cgroup_v2
            if path == docker_runner.CGROUP_V2_CONTROLLERS_FILE
            else False,
        )
        monkeypatch.setattr(
            DockerRunner,
            'get_docker_server_version',
            staticmethod(lambda: docker_version),
        )
        runner = make_runner(package_tests=PACKAGE_TESTS)
        result = runner.check_init_container_support()
        if error:
            assert re.search(error, result)
        else:
            assert result is None

    @pytest.mark.parametrize(
        'package_tests',
        [
            pytest.param(PACKAGE_TESTS, id='tmt-task'),
            pytest.param(None, id='package-task'),
        ],
    )
    def test_start_env_fails_without_support(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        package_tests: Optional[dict],
    ):
        monkeypatch.setattr(
            DockerRunner,
            'check_init_container_support',
            lambda self: 'Init containers need cgroup v2',
        )
        runner = make_runner(package_tests=package_tests, work_dir=tmp_path)
        with pytest.raises(Exception, match='Cannot start environment'):
            runner.start_env()
        assert runner.artifacts['start_environment'] == {
            'exit_code': 1,
            'stdout': '',
            'stderr': 'Init containers need cgroup v2',
        }


    @pytest.mark.parametrize(
        'dist_arch, inspect, pulled',
        [
            pytest.param(
                'i686', (0, 'linux/386\n', ''), False, id='has-the-platform',
            ),
            pytest.param(
                'i686',
                (1, '', 'image ... does not provide the specified platform'),
                True,
                id='only-other-platforms',
            ),
            pytest.param(
                'x86_64_v2', (0, 'linux/amd64\n', ''), True,
                id='other-variant',
            ),
            pytest.param(
                'aarch64', (0, 'linux/arm64\n', ''), False,
                id='arm64-without-variant',
            ),
            pytest.param(
                'x86_64', (125, '', 'unknown flag: --platform'), True,
                id='docker-28.0',
            ),
        ],
    )
    def test_ensure_image_platform(
        self,
        monkeypatch: pytest.MonkeyPatch,
        dist_arch: str,
        inspect: Tuple[int, str, str],
        pulled: bool,
    ):
        """
        x86_64 and i686 tasks share a multi-platform tag, and Terraform
        pulls only an image the worker doesn't have by name: the runner
        pulls the platform of the task when the worker lacks it.
        """
        calls = []

        def run_docker(*args, timeout=None):
            calls.append(args)
            if args[:2] == ('image', 'inspect'):
                return inspect
            return 0, 'docker.io/almalinux/9-init:9\n', ''

        monkeypatch.setattr(
            DockerRunner, 'run_docker', staticmethod(run_docker),
        )
        runner = make_runner(dist_arch=dist_arch)
        platform = docker_runner.ARCH_PLATFORM_MAPPING[dist_arch]
        assert runner.ensure_image_platform() is None
        assert calls[0] == (
            'image', 'inspect', '--platform', platform, '--format',
            docker_runner.IMAGE_PLATFORM_FORMAT, 'almalinux/9-init:9',
        )
        assert calls[1:] == (
            [('pull', '--platform', platform, 'almalinux/9-init:9')]
            if pulled else []
        )

    def test_start_env_fails_without_the_image(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        monkeypatch.setattr(
            DockerRunner, 'check_init_container_support', lambda self: None,
        )
        monkeypatch.setattr(
            DockerRunner,
            'run_docker',
            staticmethod(lambda *args, timeout=None: (1, '', 'no network')),
        )
        runner = make_runner(dist_arch='i686', work_dir=tmp_path)
        with pytest.raises(Exception, match='Cannot start environment'):
            runner.start_env()
        assert runner.artifacts['start_environment']['stderr'] == (
            'Cannot pull almalinux/9-init:9 for linux/386: no network'
        )

DISCOVERED_YAML = (
    '- name: /Sanity/basic\n'
    '- name: /Sanity/reboot\n'
    '- name: /Sanity/reboot-other\n'
)


class FakeDockerRunner(DockerRunner):
    """Runs the tmt stage against a fake environment."""

    discovered_yaml = DISCOVERED_YAML
    # tmt run: exit code and output; status of discover/step.yaml by plan
    tmt_run = (1, 'tmt output', '')
    discover_status: Dict[str, str] = {}

    def __init__(self, *args, results: Dict[str, str], tmt_help: str,
                 env_running: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands: List[Tuple[str, ...]] = []
        self.copied_to_env: List[Tuple[str, str]] = []
        self.results = results
        self.tmt_help = tmt_help
        self.env_running = env_running

    def exec_command(self, *args, workdir=None, timeout=None):
        self.commands.append(tuple(args))
        args = list(args)
        if args[0] == 'timeout':
            args = args[3:]
        if args[:2] == ['cat', '/etc/os-release']:
            return 0, 'NAME="AlmaLinux"\nVERSION_ID="9.7"\n', ''
        if args[:2] == ['tmt', '--help']:
            return 0, self.tmt_help, ''
        if args[0] == 'tmt' and 'export' in args:
            return 0, (
                '- name: /plans/tier1/public\n'
                '  discover: {how: fmf, filter: "tier: 1"}\n'
            ), ''
        if args[0] == 'cat' and args[1].endswith('/discover/tests.yaml'):
            return 0, self.discovered_yaml, ''
        if args[0] == 'tmt' and 'provision' in args:
            return self.tmt_run
        if args[0] == 'rpm':
            return 0, 'chrony-4.6-1.el9.x86_64\n', ''
        return 0, '', ''

    def copy_to_env(self, local_path: str, remote_path: str):
        self.copied_to_env.append((local_path, remote_path))
        return 0, '', ''

    def copy_from_env(self, remote_path: str, local_path: str):
        for plan, status in self.discover_status.items():
            step = Path(local_path, plan.lstrip('/'), 'discover', 'step.yaml')
            step.parent.mkdir(parents=True)
            step.write_text(f'status: {status}\ndata: []\n')
        for plan, text in self.results.items():
            plan_dir = Path(local_path, plan.lstrip('/'), 'execute')
            plan_dir.mkdir(parents=True, exist_ok=True)
            Path(plan_dir, 'results.yaml').write_text(text)
            output = Path(
                plan_dir,
                'data/guest/default-0/Sanity/broken-1/output.txt',
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('line 1\nline 2\n:: [ FAIL ] :: broken\n')
        return 0, '', ''

    def is_env_running(self) -> bool:
        return self.env_running


def make_fake_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    results: Dict[str, str],
    tmt_help: str = 'Options:\n  --feeling-safe  ...',
    env_running: bool = True,
    package_tests: Optional[dict] = None,
) -> FakeDockerRunner:
    repo = Path(tmp_path, 'tmt_tests', 'chrony')

    def fetch_git_commit(url, commit, work_dir, logger):
        assert (url, commit) == (PACKAGE_TESTS['url'], COMMIT)
        repo.mkdir(parents=True)
        return repo, ''

    monkeypatch.setattr(base_runner, 'fetch_git_commit', fetch_git_commit)
    runner = FakeDockerRunner(
        'task_id',
        task_is_aborted,
        'almalinux',
        '9',
        package_tests=package_tests or PACKAGE_TESTS,
        results=results,
        tmt_help=tmt_help,
        env_running=env_running,
    )
    runner._work_dir = str(tmp_path)
    return runner


def get_tmt_command(runner: FakeDockerRunner) -> List[str]:
    commands = [
        list(cmd) for cmd in runner.commands
        if 'tmt' in cmd and 'provision' in cmd
    ]
    assert len(commands) == 1
    return commands[0]


class TestRunTmtTests:
    def test_run_tmt_tests(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        package_tests = dict(
            PACKAGE_TESTS,
            packages=['chrony-4.6-1.el9.x86_64'],
        )
        runner = make_fake_runner(
            tmp_path,
            monkeypatch,
            {'/plans/tier1/public': RESULTS_YAML},
            package_tests=package_tests,
        )
        exit_code, stdout, stderr = runner.run_tmt_tests(
            package_tests,
            skipped_tests=[
                'uninstall_package',
                'tmt_test_/Sanity/reboot',
            ],
        )
        assert exit_code == 0, stderr
        assert runner.copied_to_env == [
            (str(Path(tmp_path, 'tmt_tests', 'chrony')),
             '/var/tmp/tmt-tests/chrony'),
        ]
        assert ('dnf', 'download', '--destdir', '/var/share/test-artifacts',
                'chrony-4.6-1.el9.x86_64') in runner.commands
        cmd = get_tmt_command(runner)
        assert cmd[:4] == ['timeout', '--kill-after=60',
                           str(CONFIG.tmt_exec_timeout), 'tmt']
        assert '--feeling-safe' in cmd
        assert cmd[cmd.index('-c') + 1] == (
            'distro=almalinux-9.7,centos-stream-9,rhel-9.7'
        )
        assert 'component=chrony' in cmd
        # The internal plan is not run, the skipped test is left out
        # of the tmt run
        plans = cmd[cmd.index('plans'):cmd.index('tests')]
        assert plans == ['plans', '--name', '^/plans/tier1/public$']
        name_regex = cmd[cmd.index('tests') + 2]
        assert not re.search(name_regex, '/Sanity/reboot')
        assert re.search(name_regex, '/Sanity/reboot-other')

        assert runner.skipped_tmt_tests == [
            'tmt_test_/Sanity/reboot',
            'tmt_test_/Sanity/upstream-skip',
        ]
        tmt = runner.artifacts['tmt']
        assert sorted(tmt) == [
            'tmt_test_/Sanity/basic',
            'tmt_test_/Sanity/broken',
            'tmt_test_/Sanity/warned',
        ]
        broken = tmt['tmt_test_/Sanity/broken']
        assert broken['exit_code'] == 1
        assert ':: [ FAIL ] :: broken' in broken['stdout']
        assert broken['tmt'] == {
            'name': '/Sanity/broken',
            'plan': '/plans/tier1/public',
            'result': 'fail',
            'duration': '',
            'note': 'failed in the test phase',
            'summary': (
                'Result: fail\nPlan: /plans/tier1/public\n'
                'Note: failed in the test phase\n\n'
                'line 1\nline 2\n:: [ FAIL ] :: broken'
            ),
        }
        assert tmt['tmt_test_/Sanity/warned']['exit_code'] == 0
        assert tmt['tmt_test_/Sanity/basic']['exit_code'] == 0
        assert runner.tmt_report == {
            'url': PACKAGE_TESTS['url'],
            'commit': COMMIT,
            'installed_packages': ['chrony-4.6-1.el9.x86_64'],
            'context': {
                'distro': 'almalinux-9.7,centos-stream-9,rhel-9.7',
                'arch': 'x86_64',
                'initiator': 'albs',
                'component': 'chrony',
            },
            'excluded_plans': [
                {'name': '/plans/tier1/internal', 'reason': 'internal plan'},
            ],
            'tmt_exit_code': 1,
            'plans': [
                {
                    'name': '/plans/tier1/public',
                    'counts': {'pass': 1, 'fail': 1, 'warn': 1, 'skip': 1},
                },
            ],
            'counts': {'pass': 1, 'fail': 1, 'warn': 1},
        }
        assert runner.artifacts['tmt_tests']['exit_code'] == 0

    @pytest.mark.parametrize(
        'skip_rule',
        [
            'tmt_test_/cups-public/Sanity/cups-systemd-units-smoke',
            'tmt_test_/Sanity/cups-systemd-units-smoke',
        ],
        ids=['with-phase-prefix', 'without-phase-prefix'],
    )
    def test_run_tmt_tests_several_discover_phases(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        skip_rule: str,
    ):
        """
        A plan with two discover phases: tmt names the tests
        /<phase>/<name> in discover and in the results, "tests --name"
        matches the name without the prefix. The skip rule in either form
        leaves the test out of the run and the result.
        """
        monkeypatch.setattr(
            FakeDockerRunner,
            'discovered_yaml',
            '- name: /cups-public/Sanity/basic-commands\n'
            '  discover-phase: cups-public\n'
            '- name: /cups-public/Sanity/cups-systemd-units-smoke\n'
            '  discover-phase: cups-public\n'
            '- name: /cups-filters-public/Sanity/smoke\n'
            '  discover-phase: cups-filters-public\n',
        )
        results_yaml = (
            '- name: /cups-public/Sanity/basic-commands\n'
            '  result: pass\n'
            '- name: /cups-filters-public/Sanity/smoke\n'
            '  result: pass\n'
        )
        runner = make_fake_runner(
            tmp_path,
            monkeypatch,
            {'/plans/tier1/public': results_yaml},
        )
        exit_code, stdout, stderr = runner.run_tmt_tests(
            PACKAGE_TESTS,
            skipped_tests=[skip_rule],
        )
        assert exit_code == 0, stderr
        cmd = get_tmt_command(runner)
        name_regex = cmd[cmd.index('tests') + 2]
        assert not re.search(name_regex, '/Sanity/cups-systemd-units-smoke')
        assert re.search(name_regex, '/Sanity/basic-commands')
        assert re.search(name_regex, '/Sanity/smoke')
        assert runner.skipped_tmt_tests == [
            'tmt_test_/cups-public/Sanity/cups-systemd-units-smoke',
        ]
        assert sorted(runner.artifacts['tmt']) == [
            'tmt_test_/cups-filters-public/Sanity/smoke',
            'tmt_test_/cups-public/Sanity/basic-commands',
        ]
        assert 'is skipped but has run' not in stdout

    @pytest.mark.parametrize(
        'skip_rule',
        [
            'tmt_test_/cups-public/Sanity/cups-systemd-units-smoke',
            'tmt_test_/Sanity/cups-systemd-units-smoke',
        ],
        ids=['with-phase-prefix', 'without-phase-prefix'],
    )
    def test_skipped_test_that_has_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        skip_rule: str,
    ):
        """
        The run has a skipped test tmt discover hasn't shown: its result
        is dropped, the rule matches it in both forms.
        """
        monkeypatch.setattr(
            FakeDockerRunner,
            'discovered_yaml',
            '- name: /cups-public/Sanity/cups-systemd-units-smoke\n'
            '  discover_phase: cups-public\n'
            '- name: /cups-filters-public/Sanity/smoke\n'
            '  discover_phase: cups-filters-public\n',
        )
        results_yaml = (
            '- name: /cups-public/Sanity/cups-systemd-units-smoke\n'
            '  result: fail\n'
            '- name: /cups-filters-public/Sanity/smoke\n'
            '  result: pass\n'
        )
        runner = make_fake_runner(
            tmp_path,
            monkeypatch,
            {'/plans/tier1/public': results_yaml},
        )
        exit_code, stdout, _ = runner.run_tmt_tests(
            PACKAGE_TESTS,
            skipped_tests=[skip_rule],
        )
        assert exit_code == 0
        assert (
            'Test /cups-public/Sanity/cups-systemd-units-smoke is skipped '
            'but has run, its result is dropped'
        ) in stdout
        assert runner.skipped_tmt_tests == [
            'tmt_test_/cups-public/Sanity/cups-systemd-units-smoke',
        ]
        assert sorted(runner.artifacts['tmt']) == [
            'tmt_test_/cups-filters-public/Sanity/smoke',
        ]
        assert runner.tmt_report['counts'] == {'pass': 1}

    def test_discover_failed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """
        The discover step of a plan has failed (a repository of a phase
        can't be cloned): tmt exits with 2 and leaves an empty
        results.yaml, the stage fails and says which plan and why.
        """
        monkeypatch.setattr(
            FakeDockerRunner, 'discover_status',
            {'/plans/tier1/public': 'todo'},
        )
        monkeypatch.setattr(
            FakeDockerRunner, 'tmt_run',
            (2, '', (
                '/plans/tier1/public\n'
                '    discover\n'
                '        how: fmf\n'
                "        fail: Command 'git clone https://git.example.org/"
                "tests/cups-filters.git' returned 128.\n"
                '    report\n'
                '        summary: no results found\n'
            )),
        )
        runner = make_fake_runner(
            tmp_path, monkeypatch, {'/plans/tier1/public': '[]\n'},
        )
        with pytest.raises(TmtTestsError):
            runner.run_tmt_tests(PACKAGE_TESTS)
        stage = runner.artifacts['tmt_tests']
        assert stage['exit_code'] == 1
        assert stage['stderr'] == (
            'tmt discover has failed in the plans: /plans/tier1/public'
        )
        assert (
            'tmt discover has failed in the plan /plans/tier1/public:\n'
            "  fail: Command 'git clone https://git.example.org/tests/"
            "cups-filters.git' returned 128."
        ) in stage['stdout']
        assert runner.tmt_report['tmt_exit_code'] == 2
        assert runner.tmt_report['plans'] == [{
            'name': '/plans/tier1/public',
            'error': (
                "discover failed: fail: Command 'git clone https://"
                "git.example.org/tests/cups-filters.git' returned 128."
            ),
        }]
        assert runner.tmt_report['counts'] == {}

    def test_error_outcome_is_not_a_discover_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """
        tmt exits with 2 for a test with the error outcome too: discover
        has finished, so the stage succeeds and the test is a failure.
        """
        monkeypatch.setattr(
            FakeDockerRunner, 'discover_status',
            {'/plans/tier1/public': 'done'},
        )
        monkeypatch.setattr(FakeDockerRunner, 'tmt_run', (2, 'tmt output', ''))
        runner = make_fake_runner(
            tmp_path, monkeypatch, {'/plans/tier1/public': (
                '- name: /Sanity/basic\n  result: pass\n'
                '- name: /Sanity/broken\n  result: error\n'
                '  note: [timeout]\n'
            )},
        )
        exit_code, stdout, stderr = runner.run_tmt_tests(PACKAGE_TESTS)
        assert exit_code == 0, stderr
        assert 'discover has failed' not in stdout
        assert runner.tmt_report['tmt_exit_code'] == 2
        assert runner.tmt_report['counts'] == {'pass': 1, 'error': 1}
        assert runner.artifacts['tmt']['tmt_test_/Sanity/broken'][
            'exit_code'
        ] == 1

    def test_run_tmt_tests_el8(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        runner = make_fake_runner(
            tmp_path,
            monkeypatch,
            {'/plans/tier1/public': '[]'},
            tmt_help='Options:\n  --root PATH',
        )
        exit_code, _, _ = runner.run_tmt_tests(PACKAGE_TESTS)
        assert exit_code == 0
        cmd = get_tmt_command(runner)
        assert '--feeling-safe' not in cmd
        # Nothing skipped: no discover run and no test name regex
        assert 'tests' not in cmd
        assert not [c for c in runner.commands if 'discover' in c]

    def test_run_tmt_tests_without_results(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        runner = make_fake_runner(
            tmp_path,
            monkeypatch,
            {},
            env_running=False,
        )
        with pytest.raises(TmtTestsError):
            runner.run_tmt_tests(PACKAGE_TESTS)
        stage = runner.artifacts['tmt_tests']
        assert stage['exit_code'] == 1
        assert 'tmt has not run the plans: /plans/tier1/public' in (
            stage['stderr']
        )
        assert 'probably a test rebooted it' in stage['stdout']

    def test_run_tmt_tests_cannot_fetch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(
            base_runner,
            'fetch_git_commit',
            lambda *args: (None, f'Commit {COMMIT} is not in the repo'),
        )
        runner = make_runner(package_tests=PACKAGE_TESTS, work_dir=tmp_path)
        with pytest.raises(TmtTestsError):
            runner.run_tmt_tests(PACKAGE_TESTS)
        assert runner.artifacts['tmt_tests']['stderr'] == (
            f'Commit {COMMIT} is not in the repo'
        )

    def test_run_tmt_tests_without_plans(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        package_tests = dict(PACKAGE_TESTS, tmt={'plans': []})
        runner = make_fake_runner(tmp_path, monkeypatch, {})
        exit_code, stdout, _ = runner.run_tmt_tests(package_tests)
        assert exit_code == 0
        assert 'No tmt plans to run' in stdout
        assert runner.commands == []


def test_log_file_base_name():
    assert base_runner.get_log_file_base_name(
        'tmt_test_/Sanity/bz123 x:y@/plans/a',
    ) == 'tmt_test__Sanity_bz123_x_y_plans_a'
    assert base_runner.get_log_file_base_name(
        'third_party_test_smoke.sh',
    ) == 'third_party_test_smoke.sh'
