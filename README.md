System overview
--

<picture>
  <img alt="Test Coverage" src="https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/andrewlukoshko/c3616635e183fcec39a92a4ffad0015b/raw/coverage-badge.json">
</picture>
<br/><br/>

AlmaLinux Test System (ALTS) - is a way to test rpm packages under realistic circumstances, on real systems with installation, launching, integrity checks, etc. If needed, it also supports third-party test scripts.

AlmaLinux Test System is designed to be a fast, scalable and easily maintainable solution for end-to-end packages testing. 

The system consists of the ALTS scheduler and the ALTS worker. The scheduler sends received test tasks to RabbitMQ queues and monitors them. The worker takes a task from the RabbitMQ queues, manages the test environment and reports task results to the Web-Server.

The process of picking up a created task is:
- Starting up a clean environment like Docker or OpenNebula with its initial configurations;
- An attempt to install a package to the system;
- The package integrity check

The system contains several parts:

- RabbitMQ as messaging broker.
- Celery as a task managing environment, using threads pool. Internal data goes through Azure.
- Scheduler web application.
- FastAPI is used as a framework for the API
- Terraform is used as a tool for managing test environments. It allows quickly adding support for new types of environments.

Mentioned tools and libraries are required for ALTS to run in current state:

- Terraform >= 0.13
- Ansible (current version is 2.9)
- RabbitMQ;
- Celery >= 5.1
- SQLite;
- Docker (for development purposes)
- Python 3 (>= 3.6)
- FastAPI
- Pytest
- Testinfra


Test system flow
--

- Web application receives a POST request with the requirements (test runner type, distribution name, version, architecture, package name, package version, additional repositories);
- Application calculates where the task should go (RabbitMQ queue) and applies the task to it;
- Celery worker that listens to the queue receives the task parameters and starts processing;
- Task executes all needed steps and saves artifacts to Azure/S3;
- Celery will save the task result (summary on the run) to the separate Azure/S3 folder;
- Web application can return task status and result by calling a separate endpoint.

Celery launch command example:

`celery -A alts.app worker --pool=threads --concurrency=10 --loglevel=DEBUG`

To launch scheduler, apply the following command:

`uvicorn scheduler.app:app`

For testing purposes, you can add the `--reload` argument to the scheduler launch command, which will enable live code to reload.

Both Celery worker and scheduler REST API services need YAML-based configs to function. Config examples are provided in the `configs` folder.

During the task execution some tests may be skipped if it specified in a particular `.json` file which can be found here: https://git.almalinux.org/almalinux/alts-exclusions/src/branch/main/skipped_tests.json

Example of a file content:
```
{
    "almalinux-8": {
        "libxml2": ["run_package_integrity_tests"],
        "curl": ["run_package_integrity_tests"],
        "bzip2": ["run_package_integrity_tests"]
    },
    "almalinux-9": {
        "libxml2": ["run_package_integrity_tests"],
        "curl": ["run_package_integrity_tests"]
    },
    "ubuntu-24.04": {
        "bzip2": ["run_package_integrity_tests"]
    }
}
```

A task can also skip tests on its own with the `skipped_tests` field of its payload. The build system fills it from the package tests config in https://git.almalinux.org/tests/<package>. It takes the names the results use: `install_package`, `package_integrity_tests`, `third_party_tests` and `uninstall_package`, or `third_party_test_<test file>` for a single third-party test, where the file name may be a shell-style pattern (`third_party_test_*.bats`). Skipped tests are listed in `skipped_tests` of the task result.

Docker environments
--

Every docker task, a package test task or a tmt task, runs in a container of an init image with systemd as PID 1 (`/sbin/init`, the `CMD` of the image), started with `--cgroupns=private --security-opt writable-cgroups=true --tmpfs /run --tmpfs /run/lock`, without `--privileged`. The worker needs cgroup v2 and Docker 28.0.0 or newer, otherwise the environment doesn't start and the `start_environment` stage says why. The initial provision waits for systemd to reach `running` or `degraded`.

x86_64, x86_64_v2 and i686 tasks share the multi-platform tags of the init images (`almalinux/9-init:9` serves `linux/amd64` and `linux/386`): the container is created for the platform of the task, and before it the runner pulls the image for that platform (`docker pull --platform`) unless the worker has it (`docker image inspect --platform`, Docker 28.1 or newer; with older Docker the image is pulled for every task).

The images come only from `docker_images` of the worker config: full names with tags of init images by `<dist_name>-<dist_version>` of the task. An entry `<dist_name>-<dist_version>-<dist_arch>` takes precedence for one architecture, and the major version is tried after the full one (`almalinux-9` serves 9.6). A task of a platform missing there fails at `prepare_work_dir` with the keys it has looked for. There are no defaults in the code:

```yaml
docker_images:
  almalinux-8: "almalinux/8-init:8"
  almalinux-9: "almalinux/9-init:9"
  almalinux-10: "almalinux/10-init:10"
  almalinux-10-kitten: "almalinux/10-kitten-init:10-kitten"
```

tmt tests
--

A task with `package_tests.tmt` in its payload is a component test task: instead of installing a single package and running its tests, it runs the tmt plans of the component's https://git.almalinux.org/tests/<package> repository, stage `tmt_tests`.

- The environment is a docker container of an init image, like for any docker task (see Docker environments above). tmt tasks always go to the docker runner.
- The initial provision installs tmt, beakerlib and the tools tests expect from CI machines, enables CRB, starts sshd and puts a comment line into `/etc/fstab` when it is missing or empty.
- The stage checks out exactly `package_tests.commit` of `package_tests.url`, installs `package_tests.packages` and puts their RPMs into `/var/share/test-artifacts`, then runs `tmt --feeling-safe -c distro=... -c arch=... -c initiator=albs run -e HOME=/root -e PATH=... --all provision --how local plans --name ...` in `/var/tmp/tmt/run` (without `--feeling-safe` where tmt doesn't know it, tmt 1.26 on AlmaLinux 8). `distro` is `almalinux-<version>,centos-stream-<major>,rhel-<version>`, for Kitten `almalinux-kitten-10,centos-stream-10`. Plans named `.../internal` or using internal Red Hat repositories are not run.
- `tmt_test_<test name>` in `skipped_tests` (a shell-style pattern, e.g. `tmt_test_/Regression/RHEL-9973-*`) leaves the test out of the tmt run itself, and `tmt_tests` skips the stage. In a plan with several discover phases tmt names a test `/<phase>/<name>` in the results, e.g. `/cups-public/Sanity/basic-commands`: a rule may give either `tmt_test_/cups-public/Sanity/basic-commands` or `tmt_test_/Sanity/basic-commands`, the second one leaves the test out of every phase.
- A failed tmt test doesn't fail the stage: the stage fails only when it can't run the plans, e.g. when the discover step of a plan fails (a repository of a discover phase can't be cloned, ...). tmt leaves `discover/step.yaml` of such a plan in the `todo` status and an empty `results.yaml`; the plan gets `"error": "discover failed: <tmt error>"` in `tmt_tests.plans`, the stage log has the tmt errors of the step and `tmt_tests.success` is false. tmt exits with 2 then, as for a test with the `error` outcome, so the exit code doesn't tell the two apart, and doesn't run the plans after it (they get `"error": "no results"`). The results of the tests are in the `tmt` section of the result, `tmt_test_<test name>`, or `tmt_test_<test name>@<plan>` for a test of several selected plans:

```ruby
"tmt_tests": {
  "success": true, # tmt has run all the plans
  "url": "https://git.almalinux.org/tests/chrony.git",
  "commit": "4f1c2e9...",
  "installed_packages": ["chrony-4.6-1.el9.x86_64"],
  "context": {"distro": "almalinux-9.7,centos-stream-9,rhel-9.7", "arch": "x86_64", "initiator": "albs", "component": "chrony"},
  "excluded_plans": [{"name": "/plans/tier1/internal", "reason": "internal plan"}],
  "tmt_exit_code": 1,
  "plans": [{"name": "/plans/tier1/public", "counts": {"pass": 7, "fail": 1}}], # or "error": "no results"
  "counts": {"pass": 7, "fail": 1}
},
"tmt": {
  "tmt_test_/Sanity/basic": {
    "success": true, # pass, info and warn are a success; fail, error and pending are not
    "result": "pass", # the tmt outcome
    "name": "/Sanity/basic",
    "plan": "/plans/tier1/public",
    "duration": "00:00:12",
    "note": "",
    "output": "Result: pass\nPlan: ..." # a short summary, the whole output is in the logs
  }
},
"skipped_tests": ["tmt_test_/Sanity/reboot"] # skipped by the task and by tmt itself
```

Filling options in the config file
--

Here is the description of what is necessary to fill in at alts_config.yaml:

```ruby
rabbitqm_host: 'rabbitmq' # hostname for the message broker
rabbitmq_port: 5672 # unprotected broker port
rabbitmq_ssl_port: 5671 # protected broker port. They are used separetly depending on the flag 'use_ssl'
rabbitmq_user: 'test-system' # the user as message broker for connection
rabbitmq_password: # the user's password
rabbitmq_vhost: 'test_system' # message "base" on broker
```

Choosing a backend in the`results backend` option, you define which parameter S3 or Azure you need:
`result_backend: 's3' #  'azureblockblob://$connection_string' # a type of results backend - S3 or connection string to Azure`

Now a group of S3 options for authorization on S3:

```ruby
s3_access_key_id:
s3_secret_access_key:
s3_bucket:
s3_base_path: 'celery_result_backend/' # path at S3 where to put artefacts
s3_region:
s3_endpoint_url: # optional, if not S3 is used, but its substitute
result_backend: 's3'
```

Options to connect to Azure:

```ruby
azureblockblob_container_name: # container name for Celery on Azure
azureblockblob_base_path: # a directory to store testing results
azure_connection_string: # connection string to Azure
azure_logs_container: # container name for logs
```

Running using docker-compose
--

You can start the whole system using the Docker Compose tool.

Pre-requisites:

- `docker` and `docker-compose` tools are installed and set up;

Few preparations needed before you start:

- Open `docker-compose.yml` and set some password in `RABBITMQ_DEFAULT_PASS` variable;
- Copy `configs/example_config.yaml` config file to `configs/alts_config.yaml`;
- Generate JWT secret: `openssl rand -hex 32`
- Fill config with Azure/AWS credentials, JWT secret and RabbitMQ password;

To start the system, run the following command: `docker-compose up -d`. This command will pull RabbitMQ image, build docker images for Celery and Scheduler containers and start the containers themselves. To rebuild images after your local changes, just run `docker-compose up -d --build`.


Deploying to servers
--

Ansible roles and playbooks for deploying RabbitMQ, the scheduler (with Flower) and Celery workers live in the [albs-deploy](https://github.com/AlmaLinux/albs-deploy) repository, see the ALTS section of its README.


Running in virtualenv
--
You can start the application inside virtualenv. Create virtualenv:

`python3 -m venv venv`

Activate virtualenv:

`source venv/bin/activate`

Install all needed packages:

`pip install -r requirements/scheduler.txt`

After that, you can start Celery and scheduler.


Scheduling tasks
--

Scheduler part provides 2 REST API endpoints:

- `POST /tasks/schedule` - to schedule package for installation/tests run;
- `GET /tasks/{task_id}/result` - to get result of the task;

Before installing packages from the module and running tests, the module from the build has to be turned on. To do so, use the following payload in the `/tasks/schedule` endpoint:

```ruby
{
  "module_name": "string" # the name of the module to install, it's an optional value that allows being absent
  "module_stream": "string" # the stream of the module to install, it's an optional value that allows being absent
  "module_version": "string" # the version of the module to install, it's an optional value that allows being absent
}
```

As scheduler uses FastAPI, it has Swagger support built-in. You can open Swagger documentation just by following the http://localhost:8000/docs link.

Authentication is achieved using JWT tokens. For ease of testing, there is a `generate_jwt_token.py` script. You can either specify a config file to parse the JWT secret and hashing algorithm or provide them via a parameter. For details on all script parameters, run python `generate_jwt_token.py --help`. Usage example with config:

`python generate_jwt_token.py -c configs/alts_config.yaml -e some@email.com`

With command line parameters:

`python generate_jwt_token.py -s my_very_secret_phrase -a HS256 -e some@email.com`

After acquiring the token, you can put it in Authorize section on the http://localhost:8000/docs page. It will open endpoints for further usage.

`/tasks/schedule` endpoint accepts the following payload:

```ruby
{
  "runner_type": "string" # the instance backend to use (docker, opennebula, etc.). For now only 'docker' and 'any'' values are supported
  "dist_name": "string" # name of distribution you want to test on (debian, ubuntu, centos, etc.)
  "dist_version": "string" # distribution version (20.04, 8, etc.)
  "dist_arch": "string" # CPU architecture you want to test package for. Supported values - 'x86_64', 'i686', 'amd64', 'aarch64', 'arm64'
  "repositories": [] # a list of repositories to add before attempting package installation. Each repository is a dictionary with 'name' and 'url' values. 'name' is optional
  "package_name": "string" # the name of the package to install
  "package_version": "string" # optional, version of the package you want to install
  "skipped_tests": [] # optional, tests not to run, e.g. ["uninstall_package", "third_party_test_*.bats"]
  "package_tests": { # optional, tmt tests of the component (see "tmt tests")
    "url": "https://git.almalinux.org/tests/chrony.git", # tests/<package> repository
    "commit": "4f1c2e9...", # full hash of the commit to test
    "packages": ["chrony-4.6-1.el9.x86_64"], # optional, build packages to install, dnf specs
    "tmt": {
      "plans": ["/plans/tier1/public"], # plans for the platform and the architecture of the task
      "filter": "tier: 1", # optional, like "tmt run tests --filter"
      "context": {"component": "chrony"}, # optional, added to distro, arch and initiator
      "environment": {"TEST_PROFILE": "container"} # optional, environment of the tests
    }
  }
}
```
  
`/tasks/{task_id}/result` endpoint returns the result of task. `task_id` - task ID string.


Unresolved issues
--
- Add support for external test projects like LTP;


Reporting issues 
--
All issues should be reported to the [Build System project](https://github.com/AlmaLinux/build-system).
