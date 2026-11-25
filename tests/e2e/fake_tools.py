"""
Fake docker, terraform, ansible-playbook and git for the flow tests of
a whole ALTS task (tests/e2e). The tests put wrappers named after the
tools first in PATH, so ALTS runs its real code and only the external
tools are fake:

- terraform apply reads the docker.tf ALTS rendered and "creates" the
  container: its settings go to <state>/containers/<name>/config.json,
  its file system is <state>/containers/<name>/fs;
- docker exec emulates the commands ALTS runs in the container, tmt
  included: the fake tmt takes the tests of a plan from the scenario
  results (real tmt results.yaml files), selects them with the plan and
  test regexes of its command line the way tmt does, and writes
  results.yaml and output.txt like tmt;
- every call is appended to <state>/calls.jsonl.

Environment: FAKE_ALTS_STATE (state directory), FAKE_ALTS_SCENARIO
(scenario directory, see tests/fixtures/tmt/README.md).

usage: fake_tools.py docker|terraform|ansible-playbook|git ARGS...
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

STATE = Path(os.environ['FAKE_ALTS_STATE'])
SCENARIO = Path(os.environ['FAKE_ALTS_SCENARIO'])

OS_RELEASES = {
    '8': 'NAME="AlmaLinux"\nVERSION="8.10 (Cerulean Leopard)"\n'
         'ID="almalinux"\nVERSION_ID="8.10"\n',
    '9': 'NAME="AlmaLinux"\nVERSION="9.9 (Chartreuse Bobcat)"\n'
         'ID="almalinux"\nVERSION_ID="9.9"\n',
    '10-kitten': 'NAME="AlmaLinux Kitten"\nVERSION="10 (Lion Cub)"\n'
                 'ID="almalinux"\nVERSION_ID="10"\n',
}
# tmt 1.26 in EPEL 8 has no --feeling-safe, tmt >= 1.77 requires it
# for "provision --how local"
TMT_HELP_OLD = 'Usage: tmt [OPTIONS] COMMAND [ARGS]...\n  --root PATH\n'
TMT_HELP_NEW = TMT_HELP_OLD + '  --feeling-safe  Allow unsafe actions.\n'


def log_call(tool, args, **extra):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / 'calls.jsonl', 'a') as fd:
        fd.write(json.dumps({'tool': tool, 'args': args, **extra}) + '\n')


def scenario():
    return yaml.safe_load((SCENARIO / 'scenario.yaml').read_text())


def container_dir(name):
    return STATE / 'containers' / name


def container_config(name):
    path = container_dir(name) / 'config.json'
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_config(name, config):
    (container_dir(name) / 'config.json').write_text(json.dumps(config))


# The images of the worker: the platforms of a tag it has locally (the
# containerd image store keeps several). A tag the scenario doesn't know
# has only linux/amd64, as on a worker that has run x86_64 tasks
HOST_PLATFORM = 'linux/amd64'


def local_images():
    path = STATE / 'images.json'
    return json.loads(path.read_text()) if path.exists() else {}


def image_platforms(image):
    return local_images().get(image, [HOST_PLATFORM])


def add_image_platform(image, platform):
    images = local_images()
    platforms = images.setdefault(image, [HOST_PLATFORM])
    if platform not in platforms:
        platforms.append(platform)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / 'images.json').write_text(json.dumps(images))


def option(args, name):
    return args[args.index(name) + 1] if name in args else None


def in_fs(name, path):
    return container_dir(name) / 'fs' / path.lstrip('/')


def out(stdout='', stderr='', code=0):
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    sys.exit(code)


# terraform

def parse_docker_tf(text):
    container = text[text.index('resource "docker_container"'):
                     text.index('resource "docker_image"')]
    image = text[text.index('resource "docker_image"'):]

    def value(block, key):
        match = re.search(rf'^\s*{key}\s*=\s*(.+)$', block, re.M)
        return match.group(1).strip() if match else None

    tmpfs = re.search(r'tmpfs = \{(.*?)\}', container, re.S)
    command = value(container, 'command')
    security_opts = value(container, 'security_opts')
    return {
        'name': json.loads(value(container, 'name')),
        'image': json.loads(value(image, 'name')),
        # Docker creates the container for this platform; the pull of
        # the image has its own
        'platform': json.loads(value(container, 'platform') or 'null'),
        'image_platform': json.loads(value(image, 'platform')),
        'cgroupns_mode': json.loads(value(container, 'cgroupns_mode') or 'null'),
        'security_opts': json.loads(security_opts) if security_opts else [],
        'tmpfs': re.findall(r'"([^"]+)" = ""', tmpfs.group(1)) if tmpfs else [],
        'command': json.loads(command) if command else None,
        'privileged': value(container, 'privileged') == 'true',
        'running': True,
    }


def terraform(args):
    log_call('terraform', args, cwd=os.getcwd())
    command = args[0]
    if command == 'init':
        out('Terraform has been successfully initialized!\n')
    tf = Path('docker.tf').read_text()
    name = parse_docker_tf(tf)['name']
    if command == 'apply':
        config = parse_docker_tf(tf)
        image = config['image']
        # The provider pulls only an image the worker doesn't have by
        # name; Docker creates the container for the platform asked or
        # for the host one
        platform = config['platform']
        if platform and platform not in image_platforms(image):
            out(stderr=(
                'Error: Unable to create container: Error response from '
                f'daemon: image with reference {image} was found but does '
                f'not provide the specified platform ({platform})\n'
            ), code=1)
        config['runtime_platform'] = platform or HOST_PLATFORM
        version = image.rsplit(':', 1)[1]
        major = '10-kitten' if 'kitten' in version else version.split('.')[0]
        config['init'] = '-init' in image
        config['tmt_help'] = TMT_HELP_OLD if major == '8' else TMT_HELP_NEW
        fs = container_dir(name) / 'fs'
        fs.mkdir(parents=True)
        save_config(name, config)
        (fs / 'etc').mkdir()
        (fs / 'etc' / 'os-release').write_text(OS_RELEASES[major])
        out('Apply complete! Resources: 2 added, 0 changed, 0 destroyed.\n')
    if command == 'output':
        out(name)
    if command == 'destroy':
        config = container_config(name)
        if config:
            config['destroyed'] = True
            save_config(name, config)
        out('Destroy complete! Resources: 2 destroyed.\n')
    out(stderr=f'unknown terraform command {args}', code=1)


# ansible-playbook

def ansible_playbook(args):
    variables = args[args.index('-e') + 1] if '-e' in args else ''
    tags = args[args.index('-t') + 1] if '-t' in args else ''
    log_call('ansible-playbook', args, tags=tags, variables=variables)
    out('PLAY RECAP\nlocalhost: ok=10 changed=5 failed=0\n')


# git

def git(args):
    log_call('git', args, cwd=os.getcwd())
    commit = scenario()['commit']
    if args[0] in ('init', 'remote'):
        out()
    if args[0] == 'fetch':
        if '--depth' in args and args[-1] != commit:
            out(stderr=f"fatal: couldn't find remote ref {args[-1]}\n",
                code=128)
        out()
    if args[0] == 'checkout':
        if args[-1] != commit:
            out(stderr=f"error: pathspec '{args[-1]}' did not match\n",
                code=1)
        Path('.fmf').mkdir(exist_ok=True)
        Path('.fmf', 'version').write_text('1\n')
        Path('HEAD.sha').write_text(commit)
        out()
    if args[:2] == ['rev-parse', 'HEAD']:
        out(Path('HEAD.sha').read_text() + '\n')
    out(stderr=f'unknown git command {args}', code=1)


# docker

def docker(args):
    log_call('docker', args)
    command = args[0]
    if command == 'version':
        out('29.10.0\n')
    if args[:2] == ['image', 'inspect']:
        platform, image = option(args, '--platform'), args[-1]
        if platform not in image_platforms(image):
            out(stderr=(
                f'Error response from daemon: image with reference {image} '
                f'was found but does not provide the specified platform '
                f'({platform})\n'
            ), code=1)
        out(platform + '\n')
    if command == 'pull':
        image = args[-1]
        add_image_platform(image, option(args, '--platform'))
        out(f'docker.io/{image}\n')
    if command == 'inspect':
        config = container_config(args[-1])
        if not config or config.get('destroyed'):
            out(stderr=f'Error: No such object: {args[-1]}\n', code=1)
        out('true\n' if config['running'] else 'false\n')
    if command == 'cp':
        src, dst = args[1], args[2]
        if ':' in src:
            name, path = src.split(':', 1)
            src_path, dst_path = in_fs(name, path), Path(dst)
        else:
            name, path = dst.split(':', 1)
            src_path, dst_path = Path(src), in_fs(name, path)
        if not src_path.exists():
            out(stderr=f'Error: No such file or directory: {src}\n', code=1)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_path, dst_path)
        out()
    if command == 'rm':
        out(args[-1] + '\n')
    if command == 'exec':
        docker_exec(args[1:])
    out(stderr=f'unknown docker command {args}', code=1)


def docker_exec(args):
    options = {}
    while args[0].startswith('-'):
        options.setdefault(args[0], []).append(args[1])
        args = args[2:]
    name, cmd = args[0], args[1:]
    config = container_config(name)
    if not config or config.get('destroyed'):
        out(stderr=f'Error: No such container: {name}\n', code=1)
    if not config['running']:
        out(stderr=f'Error: container {name} is not running\n', code=1)
    if cmd[0] == 'timeout':
        cmd = cmd[3:]
    if cmd[0] == 'cat':
        path = in_fs(name, cmd[1])
        if not path.is_file():
            out(stderr=f'cat: {cmd[1]}: No such file or directory\n', code=1)
        out(path.read_text())
    if cmd[:2] == ['mkdir', '-p']:
        in_fs(name, cmd[2]).mkdir(parents=True, exist_ok=True)
        out()
    if cmd[0] == 'dnf' and cmd[1] == 'download':
        destdir = in_fs(name, cmd[cmd.index('--destdir') + 1])
        destdir.mkdir(parents=True, exist_ok=True)
        for spec in cmd[cmd.index('--destdir') + 2:]:
            (destdir / f'{spec}.rpm').write_text('rpm')
        out()
    if cmd[0] == 'bash' and 'install' in cmd[2]:
        artifacts = in_fs(name, '/var/share/test-artifacts')
        installed = sorted(p.name[:-4] for p in artifacts.glob('*.rpm'))
        config['installed'] = installed
        save_config(name, config)
        out('Complete!\n')
    if cmd[0] == 'rpm' and cmd[1] == '-q':
        installed = config.get('installed', [])
        lines = [
            spec if spec in installed else f'package {spec} is not installed'
            for spec in cmd[2:]
        ]
        out('\n'.join(lines) + '\n')
    if cmd[0] == 'tmt':
        tmt(name, config, cmd[1:], options.get('--workdir', [''])[0])
    # lsblk, df, rpm -qa, py.test... in the regular stages
    out()


# tmt

def plan_results_dir(plan):
    return SCENARIO / 'results' / plan.lstrip('/') / 'execute'


def plan_tests(plan):
    path = plan_results_dir(plan) / 'results.yaml'
    if not path.exists():
        return None
    # Tests a real run didn't report because ALTS left them out of it
    extra = scenario().get('extra_tests', {}).get(plan, [])
    return (yaml.safe_load(path.read_text()) or []) + extra


def plan_phases(plan):
    """Discover phases of a plan, scenario 'phases' (default-0 if none)."""
    return scenario().get('phases', {}).get(plan) or ['default-0']


def split_phase(plan, test_name):
    """
    Discover phase of a test and the name "tmt run tests --name" sees.
    With several discover phases tmt names a test /<phase>/<name> in
    discover and in the results, but applies "tests --name" inside the
    phase, to the name without the prefix.
    """
    phases = plan_phases(plan)
    if len(phases) > 1:
        for phase in phases:
            if test_name.startswith(f'/{phase}/'):
                return phase, test_name[len(phase) + 1:]
        raise AssertionError(f'{test_name} has no phase of {plan}')
    return phases[0], test_name


def tmt(name, config, args, workdir):
    if args == ['--help']:
        out(config['tmt_help'])
    if args[0] == '--feeling-safe' and '--feeling-safe' not in config['tmt_help']:
        out(stderr='Error: No such option: --feeling-safe\n', code=2)
    feeling_safe = args[0] == '--feeling-safe'
    if feeling_safe:
        args = args[1:]
    context = {}
    while args[0] == '-c':
        key, value = args[1].split('=', 1)
        context[key] = value
        args = args[2:]
    if args[:2] == ['plans', 'export']:
        exported = [
            plan for plan in scenario().get('plans_export', [])
            if any(re.search(regex, plan['name']) for regex in args[4:])
        ]
        out(yaml.safe_dump(exported))
    assert args[0] == 'run', args
    run_id = args[args.index('--id') + 1]
    environment = [
        args[i + 1] for i, arg in enumerate(args) if arg == '-e'
    ]
    plan_regexes, test_regexes, test_filters = [], [], []
    section = None
    i = args.index('--id') + 2
    while i < len(args):
        arg = args[i]
        if arg in ('plans', 'tests'):
            section = arg
        elif arg == '--name':
            (plan_regexes if section == 'plans' else test_regexes).append(
                args[i + 1],
            )
            i += 1
        elif arg == '--filter':
            test_filters.append(args[i + 1])
            i += 1
        i += 1
    discover_only = 'discover' in args and '--all' not in args
    if not discover_only and not feeling_safe and (
        '--feeling-safe' in config['tmt_help']
    ):
        out(stderr='Error: provision --how local requires --feeling-safe\n',
            code=2)
    plans = [
        plan for plan in scenario()['plans']
        if any(re.search(regex, plan) for regex in plan_regexes)
    ]
    executed = {}
    outcomes = []
    errors = []
    discover_fail = scenario().get('discover_fail', {})
    for plan in plans:
        if not config['running']:
            # tmt was killed with the container, later plans don't run
            break
        if not discover_only and plan in discover_fail:
            # tmt saves discover/step.yaml as "todo" when it wakes the
            # step up and "done" only once discover has finished; a
            # failed discover leaves "todo" and an empty results.yaml
            plan_dir = in_fs(name, run_id) / plan.lstrip('/')
            write_step_status(plan_dir, 'todo')
            (plan_dir / 'execute').mkdir(parents=True)
            (plan_dir / 'execute' / 'results.yaml').write_text('[]\n')
            errors.append(
                f'{plan}\n    discover\n        how: fmf\n'
                f'        fail: {discover_fail[plan]}\n'
                '    report\n        summary: no results found\n'
            )
            executed[plan] = []
            # tmt stops there, later plans don't run (tmt 1.78)
            break
        tests = [
            test for test in plan_tests(plan)
            if all(
                re.search(regex, split_phase(plan, test['name'])[1])
                for regex in test_regexes
            )
        ]
        plan_dir = in_fs(name, run_id) / plan.lstrip('/')
        if discover_only:
            # tmt 1.26 writes discover_phase, newer tmt discover-phase
            phase_key = (
                'discover-phase' if '--feeling-safe' in config['tmt_help']
                else 'discover_phase'
            )
            (plan_dir / 'discover').mkdir(parents=True)
            (plan_dir / 'discover' / 'tests.yaml').write_text(yaml.safe_dump(
                [{'name': test['name'], 'path': test['name'],
                  phase_key: split_phase(plan, test['name'])[0]}
                 for test in tests],
            ))
            continue
        write_step_status(plan_dir, 'done')
        executed[plan] = [test['name'] for test in tests]
        outcomes.extend(
            result['result']
            for result in write_results(name, config, plan_dir, tests, plan)
        )
    log_call(
        'tmt', args, workdir=workdir, context=context,
        environment=environment, feeling_safe=feeling_safe,
        plan_regexes=plan_regexes, test_regexes=test_regexes,
        test_filters=test_filters, discover_only=discover_only,
        executed=executed,
    )
    if discover_only:
        out()
    if not config['running']:
        out('Aborted!\n', code=143)
    if errors:
        # tmt exits with 2 on an error, as for a test with the error
        # outcome
        out(f'summary: {len(outcomes)} tests executed\n',
            '\n'.join(errors) + '\nplan failed.\n', code=2)
    out(f'summary: {len(outcomes)} tests executed\n',
        code=0 if all(o in ('pass', 'info', 'skip') for o in outcomes) else 1)


def write_step_status(plan_dir, status):
    """discover/step.yaml of a plan, as tmt saves it."""
    step = plan_dir / 'discover' / 'step.yaml'
    step.parent.mkdir(parents=True, exist_ok=True)
    step.write_text(yaml.safe_dump({'status': status, 'data': []}))


def write_results(name, config, plan_dir, tests, plan):
    """Writes execute/results.yaml and output.txt logs of a plan.

    A log the scenario has (a real output.txt of the test) is copied,
    the others are made up.
    """
    execute = plan_dir / 'execute'
    execute.mkdir(parents=True)
    reboot = scenario().get('reboot')
    results = []
    for test in tests:
        result = dict(test)
        if not config['running']:
            # tmt was killed with the container: unfinished tests stay
            result.update({'result': 'pending', 'log': []})
            results.append(result)
            continue
        for log in result.get('log') or []:
            log_path = execute / log
            log_path.parent.mkdir(parents=True, exist_ok=True)
            real_log = plan_results_dir(plan) / log
            if real_log.is_file():
                shutil.copyfile(real_log, log_path)
                continue
            log_path.write_text(
                f':: [   LOG    ] :: {test["name"]}\n'
                f':: [  {test["result"].upper():^6}  ] :: RESULT: '
                f'{test["name"]}\n'
            )
        results.append(result)
        if test['name'] == reboot:
            config['running'] = False
            save_config(name, config)
    (execute / 'results.yaml').write_text(yaml.safe_dump(results))
    return results


TOOLS = {
    'docker': docker,
    'terraform': terraform,
    'ansible-playbook': ansible_playbook,
    'git': git,
}

if __name__ == '__main__':
    TOOLS[sys.argv[1]](sys.argv[2:])
