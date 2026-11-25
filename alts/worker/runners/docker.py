# -*- mode:python; coding:utf-8; -*-
# author: Vasily Kleschov <vkleschov@cloudlinux.com>
# created: 2021-05-13

"""AlmaLinux Test System docker environment runner."""

import os
import re
from pathlib import Path
from typing import (
    Callable,
    Dict,
    List,
    Optional,
    Union,
    Tuple,
)

from plumbum import local

from alts.shared.exceptions import (
    PackageIntegrityTestsError,
    ProvisionError,
    ThirdPartyTestError,
)
from alts.shared.uploaders.base import BaseLogsUploader
from alts.worker import CONFIG
from alts.worker.executors.ansible import AnsibleExecutor
from alts.worker.executors.bats import BatsExecutor
from alts.worker.executors.command import CommandExecutor
from alts.worker.executors.shell import ShellExecutor
from alts.worker.runners.base import (
    TESTS_SECTION_NAME,
    BaseRunner,
    command_decorator,
)

__all__ = ['DockerImageNotConfigured', 'DockerRunner']

# writable-cgroups appeared in Docker 28.0.0 (moby PR #48828)
INIT_CONTAINER_MIN_DOCKER_VERSION = (28, 0)
CGROUP_V2_CONTROLLERS_FILE = '/sys/fs/cgroup/cgroup.controllers'
IMAGE_PULL_TIMEOUT = 1800  # 30 minutes in seconds
# "linux/386", "linux/amd64/v2", like ARCH_PLATFORM_MAPPING
IMAGE_PLATFORM_FORMAT = (
    '{{.Os}}/{{.Architecture}}{{with .Variant}}/{{.}}{{end}}'
)


def same_platform(first: str, second: str) -> bool:
    """linux/arm64 is linux/arm64/v8, images often omit the variant."""
    def normalize(platform: str) -> str:
        return platform[:-len('/v8')] if platform.endswith(
            'arm64/v8'
        ) else platform

    return normalize(first) == normalize(second)


class DockerImageNotConfigured(ValueError):
    """The worker config has no image for the platform of the task."""


ARCH_PLATFORM_MAPPING = {
    'i386': 'linux/386',
    'i486': 'linux/386',
    'i586': 'linux/386',
    'i686': 'linux/386',
    'amd64': 'linux/amd64',
    'x86_64': 'linux/amd64',
    'x86_64_v2': 'linux/amd64/v2',
    'arm64': 'linux/arm64/v8',
    'aarch64': 'linux/arm64/v8',
    'ppc64le': 'linux/ppc64le',
    's390x': 'linux/s390x',
}


class DockerRunner(BaseRunner):
    """Docker environment runner for testing tasks."""

    TYPE = 'docker'
    TF_MAIN_FILE = 'docker.tf'
    TEMPFILE_PREFIX = 'docker_test_runner_'

    def __init__(
        self,
        task_id: str,
        task_is_aborted: Callable,
        dist_name: str,
        dist_version: Union[str, int],
        repositories: Optional[List[dict]] = None,
        dist_arch: str = 'x86_64',
        test_configuration: Optional[dict] = None,
        test_flavor: Optional[Dict[str, str]] = None,
        vm_alive: bool = False,
        artifacts_uploader: Optional[BaseLogsUploader] = None,
        package_channel: Optional[str] = None,
        verbose: bool = False,
        package_tests: Optional[dict] = None,
    ):
        """
        Docker environment class initialization.

        Parameters
        ----------
        task_id : str
            Test System task identifier.
        dist_name : str
            Distribution name.
        dist_version : str, int
            Distribution version.
        repositories : list of dict
            List of packages' repositories/
        dist_arch : str
            Distribution architecture.
        """
        super().__init__(
            task_id,
            task_is_aborted,
            dist_name,
            dist_version,
            repositories=repositories,
            dist_arch=dist_arch,
            test_configuration=test_configuration,
            test_flavor=test_flavor,
            vm_alive=vm_alive,
            artifacts_uploader=artifacts_uploader,
            package_channel=package_channel,
            verbose=verbose,
            package_tests=package_tests,
        )
        self._ansible_connection_type = 'docker'
        self._docker_image: Optional[str] = None

    def get_docker_image_keys(self) -> List[str]:
        """
        Keys of docker_images in the config for the task, the first
        found wins: with the architecture, then without it; the full
        distribution version, then its major part.
        """
        versions = [self.dist_version]
        major_version = self.dist_version.split('.')[0]
        if major_version != self.dist_version:
            versions.append(major_version)
        keys = [
            f'{self.dist_name}-{version}-{self.dist_arch}'
            for version in versions
        ]
        keys.extend(f'{self.dist_name}-{version}' for version in versions)
        return keys

    @property
    def docker_image(self) -> str:
        """
        The init image of the task from docker_images of the config, the
        only source of the images. Raises DockerImageNotConfigured if the
        platform isn't there.
        """
        if self._docker_image:
            return self._docker_image
        keys = self.get_docker_image_keys()
        image = next(
            (
                CONFIG.docker_images[key]
                for key in keys
                if key in CONFIG.docker_images
            ),
            None,
        )
        if not image:
            raise DockerImageNotConfigured(
                f'There is no docker image for {self.dist_name} '
                f'{self.dist_version} {self.dist_arch} in docker_images '
                f'of the worker config, looked for: {", ".join(keys)}'
            )
        self._docker_image = image
        return image

    @property
    def init_container(self) -> bool:
        """
        Docker environments always run in init images with systemd as
        PID 1 (D-21, ALTS-11).
        """
        return True

    def _render_tf_main_file(self):
        """
        Renders Terraform file for creating a template.

        Raises
        ------
        ValueError
            Raised if cannot map distribution architecture
            with image architecture.
        """
        docker_tf_file = os.path.join(self._work_dir, self.TF_MAIN_FILE)
        image_platform = ARCH_PLATFORM_MAPPING.get(self.dist_arch)
        image_name = self.docker_image
        external_network = os.environ.get('EXTERNAL_NETWORK', None)
        http_proxy = os.environ.get('http_proxy', None)
        https_proxy = os.environ.get('https_proxy', None)
        no_proxy = os.environ.get('no_proxy', None)

        self._render_template(
            f'{self.TF_MAIN_FILE}.tmpl',
            docker_tf_file,
            container_name=self.env_name,
            external_network=external_network,
            dist_name=self.dist_name,
            image_name=image_name,
            image_platform=image_platform,
            http_proxy=http_proxy,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
        )

    @staticmethod
    def get_docker_server_version() -> Tuple[int, str]:
        exit_code, stdout, _ = local['docker'].run(
            ['version', '--format', '{{.Server.Version}}'],
            retcode=None,
        )
        return exit_code, stdout.strip()

    @staticmethod
    def parse_docker_version(version: str) -> Optional[Tuple[int, int]]:
        match = re.match(r'(\d+)\.(\d+)', version)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    def check_init_container_support(self) -> Optional[str]:
        """
        An init container needs cgroup v2 on the worker and Docker
        >= 28.0.0 with --security-opt writable-cgroups (ALTS-16).
        Returns the error or None.
        """
        if not os.path.exists(CGROUP_V2_CONTROLLERS_FILE):
            return (
                'Init containers need cgroup v2 on the worker: '
                f'{CGROUP_V2_CONTROLLERS_FILE} does not exist, '
                '/sys/fs/cgroup is not cgroup2fs'
            )
        exit_code, version = self.get_docker_server_version()
        parsed = self.parse_docker_version(version)
        if exit_code != 0 or not parsed:
            return f'Cannot get the Docker server version: {version}'
        if parsed < INIT_CONTAINER_MIN_DOCKER_VERSION:
            required = '.'.join(map(str, INIT_CONTAINER_MIN_DOCKER_VERSION))
            return (
                f'Init containers need Docker >= {required} on the worker '
                f'(--security-opt writable-cgroups), it is {version}'
            )
        return None

    @staticmethod
    def run_docker(
        *args: str,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        return local['docker'].run(args=args, retcode=None, timeout=timeout)

    def ensure_image_platform(self) -> Optional[str]:
        """
        Pulls the image for the platform of the task unless the worker
        has it. x86_64 and i686 tasks share the tags of the init images
        (one tag, several platforms), and the docker_image resource of
        Terraform pulls only an image the worker doesn't have by name,
        whatever platforms it has, so an i686 task could find only the
        x86_64 image of the tag. "docker image inspect --platform" needs
        Docker 28.1; with older Docker the image is always pulled.
        Returns the error or None.
        """
        platform = ARCH_PLATFORM_MAPPING.get(self.dist_arch)
        if not platform:
            return None
        image = self.docker_image
        exit_code, stdout, _ = self.run_docker(
            'image', 'inspect', '--platform', platform,
            '--format', IMAGE_PLATFORM_FORMAT, image,
        )
        if exit_code == 0 and same_platform(stdout.strip(), platform):
            return None
        self._logger.info('Pulling %s for %s', image, platform)
        exit_code, stdout, stderr = self.run_docker(
            'pull', '--platform', platform, image,
            timeout=IMAGE_PULL_TIMEOUT,
        )
        if exit_code != 0:
            return f'Cannot pull {image} for {platform}: {stderr or stdout}'
        return None

    def _start_env(self):
        error = self.check_init_container_support()
        if not error:
            error = self.ensure_image_platform()
        if error:
            self._logger.error(error)
            return 1, '', error
        return super()._start_env()

    def _render_tf_variables_file(self):
        pass

    def exec_command(
        self,
        *args,
        workdir: Optional[str] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        """
        Executes a command inside docker container.

        Parameters
        ----------
        args : tuple
            Arguments to create a command and its arguments to execute
            inside the container.
        workdir : str, optional
            Working directory

        Returns
        -------
        tuple
            Executed command exit code, standard output and standard error.
        """
        cmd = ['exec']
        if workdir:
            cmd.extend(['--workdir', workdir])
        cmd.extend([str(self.env_name), *args])
        self._logger.debug(
            'Running "docker %s" command',
            ' '.join(cmd),
        )
        return local['docker'].with_cwd(self._work_dir).run(
            args=tuple(cmd),
            retcode=None,
            timeout=timeout,
        )

    def copy_to_env(
        self,
        local_path: str,
        remote_path: str,
    ) -> Tuple[int, str, str]:
        return local['docker'].run(
            ['cp', local_path, f'{self.env_name}:{remote_path}'],
            retcode=None,
        )

    def copy_from_env(
        self,
        remote_path: str,
        local_path: str,
    ) -> Tuple[int, str, str]:
        # docker cp works with a stopped container too
        return local['docker'].run(
            ['cp', f'{self.env_name}:{remote_path}', local_path],
            retcode=None,
        )

    def is_env_running(self) -> bool:
        exit_code, stdout, _ = local['docker'].run(
            ['inspect', '--format', '{{.State.Running}}', str(self.env_name)],
            retcode=None,
        )
        return exit_code == 0 and stdout.strip() == 'true'

    @staticmethod
    def copy(copy_args: List[str]):
        """
        Copies file/dir into docker container.
        """
        local['docker'].run(
            ['cp', *copy_args],
            retcode=None,
        )

    @command_decorator(
        'initial_provision',
        'Cannot run initial provision',
        exception_class=ProvisionError,
    )
    def initial_provision(self, verbose=False):
        """
        Creates initial provision inside docker container.

        Parameters
        ----------
        verbose : bool
            True if additional output information is needed, False otherwise.

        Returns
        -------
        tuple
            Executed command exit code, standard output and standard error.
        Raises
        ------
        ProvisionError
            Raised if error occurred with package manager updating
            or installing.
        """
        # Installing python3 package before running Ansible
        # This is needed because Debian/Ubuntu docker images
        # may not have python as pre-installed package
        if self._dist_name in CONFIG.debian_flavors:
            # Debian 9 has its repos in archive now + the archive
            # does not contain updates repository, so hacking sources.list
            if self.dist_name == 'debian' and self.dist_version.startswith('9'):
                self.exec_command(
                    'sed',
                    '-E',
                    '-i',
                    r's/.*(stretch-updates).*//',
                    '/etc/apt/sources.list',
                )
                self.exec_command(
                    'sed',
                    '-E',
                    '-i',
                    r's/(deb|security)\.debian\.org/archive\.debian\.org/',
                    '/etc/apt/sources.list',
                )
            self._logger.info('Installing python3 package...')
            exit_code, stdout, stderr = self.exec_command(
                self.pkg_manager, 'update',
            )
            if exit_code != 0:
                return exit_code, stdout, stderr
            cmd_args = (self.pkg_manager, 'install', '-y', 'python3')
            exit_code, stdout, stderr = self.exec_command(*cmd_args)
            if exit_code != 0:
                return exit_code, stdout, stderr
            self._logger.info('Installation is completed')
        if self.dist_name in CONFIG.rhel_flavors and self.dist_version == '6':
            self._logger.info('Removing old repositories')
            self.exec_command(
                'find', '/etc/yum.repos.d', '-type', 'f', '-exec',
                'rm', '-f', '{}', '+',
            )
        return super().initial_provision(verbose=verbose)

    @command_decorator(
        'package_integrity_tests',
        'Package integrity tests failed',
        exception_class=PackageIntegrityTestsError,
        additional_section_name=TESTS_SECTION_NAME,
    )
    def run_package_integrity_tests(
        self,
        package_name: str,
        package_version: Optional[str] = None,
    ):
        """
        Run basic integrity tests for the package

        Parameters
        ----------
        package_name:       str
            Package name
        package_version:    str
            Package version

        Returns
        -------
        tuple
            Exit code, stdout and stderr from executed command

        """
        tests_dir_basename = os.path.basename(self._integrity_tests_dir)
        remote_tests_path = os.path.join(
            CONFIG.tests_base_dir,
            tests_dir_basename,
        )
        cmd_args = ['py.test', '--tap-stream', '--package-name', package_name]
        if package_version:
            full_pkg_name = f'{package_name}-{package_version}'
            cmd_args.extend(['--package-version', package_version])
        else:
            full_pkg_name = package_name
        cmd_args.append('tests')
        self._logger.info(
            'Running package integrity tests for %s on %s...',
            full_pkg_name,
            self.env_name,
        )
        return self.exec_command(*cmd_args, workdir=remote_tests_path)

    @command_decorator(
        '',
        'Third party tests failed',
        exception_class=ThirdPartyTestError,
    )
    def run_third_party_test(
        self,
        executor: Union[AnsibleExecutor, BatsExecutor, CommandExecutor, ShellExecutor],
        cmd_args: List[str],
        docker_args: Optional[List[str]] = None,
        workdir: str = '',
        artifacts_key: str = '',
        additional_section_name: str = '',
        env_vars: Optional[List[str]] = None,
    ):
        return executor.run_docker_command(
            cmd_args=cmd_args,
            docker_args=docker_args,
            env_vars=env_vars,
        ).model_dump().values()

    @command_decorator(
        '',
        'Third party tests failed',
        exception_class=ThirdPartyTestError,
    )
    def run_third_party_test(
        self,
        executor: Union[AnsibleExecutor, BatsExecutor, CommandExecutor, ShellExecutor],
        cmd_args: List[str],
        docker_args: Optional[List[str]] = None,
        workdir: str = '',
        artifacts_key: str = '',
        additional_section_name: str = '',
        env_vars: Optional[List[str]] = None,
    ):
        return executor.run_docker_command(
            cmd_args=cmd_args,
            docker_args=docker_args,
            env_vars=env_vars,
        ).model_dump().values()

    def clone_third_party_repo(
        self,
        repo_url: str,
        git_ref: str,
    ) -> Optional[Path]:
        test_repo_path = super().clone_third_party_repo(repo_url, git_ref)
        if not test_repo_path:
            return
        self._logger.info('Copying tests to container')
        self._logger.debug('Repo path: %s', test_repo_path)
        self.exec_command('mkdir', '-p', CONFIG.tests_base_dir)
        self.copy([
            str(test_repo_path),
            f'{self.env_name}:{CONFIG.tests_base_dir}/{test_repo_path.name}',
        ])
        return test_repo_path

    def _stop_env(self):
        _, container_id, _ = local['terraform'].with_env(TF_LOG='TRACE').with_cwd(
            self._work_dir).run(
            args=('output', '-raw', '-no-color', 'container_id'),
            retcode=None,
            timeout=CONFIG.provision_timeout,
        )
        exit_code, out, err = super()._stop_env()
        if exit_code != 0:
            return self.exec_command('rm', '-f', container_id)
        return exit_code, out, err
