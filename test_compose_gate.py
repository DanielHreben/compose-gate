"""Unit tests for compose_gate.py. Run: python3 -m unittest -v

They run in the package build (doCheck), so every system that builds
compose-gate runs them. The NixOS VM test (tests/vm.nix) covers the real
sshd, sudo and docker around the tool.
"""
import fcntl
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import compose_gate as gate

STACK = {"minusbot", "bgutil"}

ACCEPTED = [
    "ps",
    "ps minusbot",
    "logs",
    "logs minusbot",
    "logs --tail 5",
    "logs --since 10m bgutil",
    "logs --follow --since 2h --tail 9999 --timestamps minusbot bgutil",
    "pull",
    "pull --quiet",
    "pull minusbot",
    "up --detach --wait",
    "up --detach --force-recreate --wait",
    "up --detach --wait minusbot",
    "restart",
    "restart bgutil",
]

REFUSED = [
    # not a command, or shell syntax
    "id", "sh", "bash -i", "exec minusbot sh", "run minusbot",
    "cp minusbot:/x /etc/x", "config", "down", "build", "create", "kill",
    "logs; id", "logs $(id)", "logs `id`", "update && reboot", "logs | sh",
    "logs > /etc/x", "help me", "PS", "docker compose ps",
    # global options the tool owns
    "-f /tmp/x.yml up --detach --wait", "--file x ps", "-p other ps",
    "--project-directory / ps", "--env-file /tmp/e ps", "ps -f /tmp/x",
    # other spellings of allowed options
    "logs --tail=5", "logs -n 5", "logs -f", "logs -ft", "logs -- minusbot",
    "logs --tail 5 --follow", "logs --timestamps --follow",
    "up --wait --detach", "up -d --wait", "up --detach", "up --wait",
    "up --detach --wait --build", "up --detach --wait --force-recreate",
    "pull --quiet --quiet", "logs --follow --follow",
    # bad values
    "logs --tail 0", "logs --tail 10000", "logs --tail -1", "logs --tail",
    "logs --tail all", "logs --tail 05", "logs --since 1d", "logs --since",
    "logs --since 0m", "logs --since -1h", "logs --since --follow",
    "logs --tail --follow",
    # bad services
    "logs -x", "logs .x", "logs _x", "logs ..", "logs ../etc",
    "logs minusbot minusbot", "restart --timeout 0",
]

ROOT_ONLY_REFUSED = [
    "logs portainer", "restart watchtower", "up --detach --wait nginx-proxy",
]


def words(command):
    return shlex.split(command)


def config(docker="/nonexistent/docker", **user):
    entry = {"project": "minusbot", "directory": "/srv/minusbot",
             "file": "docker-compose.yml", "docker_config": "/root/.docker"}
    entry.update(user)
    return {"sudo": "/run/wrappers/bin/sudo",
            "self": "/run/current-system/sw/bin/compose-gate",
            "docker": docker, "path": "/run/current-system/sw/bin",
            "users": {"illia": entry}}


class Grammar(unittest.TestCase):
    def test_accepted_by_both_halves(self):
        for command in ACCEPTED:
            with self.subTest(command=command):
                gate.check(words(command))
                gate.check(words(command), STACK)

    def test_refused_by_both_halves(self):
        for command in REFUSED:
            with self.subTest(command=command):
                for services in (None, STACK):
                    with self.assertRaises(gate.Refused):
                        gate.check(words(command), services)

    def test_services_checked_against_the_project_in_the_root_half(self):
        for command in ROOT_ONLY_REFUSED:
            with self.subTest(command=command):
                gate.check(words(command))
                with self.assertRaises(gate.Refused):
                    gate.check(words(command), STACK)

    def test_help(self):
        gate.check([])
        gate.check(["help"])

    def test_usage_lists_every_command_and_option(self):
        for command, options in gate.COMMANDS.items():
            self.assertIn("  " + command, gate.USAGE)
            for option, _, _ in options:
                self.assertIn(option, gate.USAGE)


class UnprivilegedHalf(unittest.TestCase):
    def run_as(self, raw, user="illia"):
        env = {"SSH_ORIGINAL_COMMAND": raw,
               "SSH_CONNECTION": "192.168.88.12 5555 192.168.88.100 22"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(gate, "log"), \
                mock.patch.object(gate.pwd, "getpwuid",
                                  return_value=types.SimpleNamespace(
                                      pw_name=user)), \
                mock.patch.object(gate.os, "execv") as execv, \
                mock.patch.object(sys, "argv", ["compose-gate", "-c", "sh"]), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.object(sys, "stdout", io.StringIO()):
            status = gate.as_user(config())
        return status, execv

    def test_accepted_command_goes_to_sudo_unchanged(self):
        status, execv = self.run_as("logs --tail 5 minusbot")
        sudo = "/run/wrappers/bin/sudo"
        execv.assert_called_once_with(sudo, [
            sudo, "-n", "/run/current-system/sw/bin/compose-gate",
            "logs", "--tail", "5", "minusbot"])

    def test_refused_command_runs_nothing(self):
        for raw in ["logs; id", "x'unbalanced", "logs " * 50, "-f x ps"]:
            with self.subTest(raw=raw):
                status, execv = self.run_as(raw)
                self.assertEqual(status, 2)
                execv.assert_not_called()

    def test_unconfigured_user_runs_nothing(self):
        status, execv = self.run_as("ps", user="mallory")
        self.assertEqual(status, 2)
        execv.assert_not_called()

    def test_empty_command_prints_help(self):
        status, execv = self.run_as("")
        self.assertEqual(status, 0)
        execv.assert_not_called()


class PrivilegedHalf(unittest.TestCase):
    """Runs the root half against a fake docker that records its argv and
    environment, and answers `config --services` with STACK."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.calls = os.path.join(self.dir, "calls")
        self.docker = os.path.join(self.dir, "docker")
        with open(self.docker, "w") as f:
            f.write("#!%s\n" % sys.executable + (
                "import json, os, sys\n"
                "args = sys.argv[1:]\n"
                "with open(%r, 'a') as f:\n"
                "    f.write(json.dumps([args, dict(os.environ)]) + '\\n')\n"
                "if args[-2:] == ['config', '--services']:\n"
                "    if os.path.exists(%r): sys.exit(1)\n"
                "    print('minusbot'); print('bgutil')\n"
            ) % (self.calls, os.path.join(self.dir, "broken")))
        os.chmod(self.docker, 0o755)
        self.config = config(docker=self.docker)
        patches = [
            mock.patch.object(gate, "LOCK_DIR", self.dir),
            mock.patch.object(gate, "log"),
            mock.patch.dict(os.environ, {"SUDO_USER": "illia"}, clear=True),
            mock.patch.object(sys, "stderr", io.StringIO()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def as_root(self, command):
        return gate.as_root(self.config, words(command))

    def recorded(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [json.loads(line) for line in f]

    def test_runs_exactly_the_words_after_the_configured_project(self):
        self.assertEqual(self.as_root("logs --tail 5 bgutil"), 0)
        (config_call, _), (args, env) = self.recorded()
        fixed = ["compose", "--project-name", "minusbot",
                 "--project-directory", "/srv/minusbot",
                 "--file", "/srv/minusbot/docker-compose.yml"]
        self.assertEqual(config_call, fixed + ["config", "--services"])
        self.assertEqual(args, fixed + ["logs", "--tail", "5", "bgutil"])
        self.assertEqual(env.get("PATH"), "/run/current-system/sw/bin")
        self.assertEqual(env.get("HOME"), "/root")
        self.assertEqual(env.get("DOCKER_CONFIG"), "/root/.docker")
        self.assertNotIn("SUDO_USER", env)
        self.assertFalse([k for k in env if k.startswith("COMPOSE")])

    def test_docker_config_is_optional(self):
        self.config["users"]["illia"].pop("docker_config")
        self.assertEqual(self.as_root("ps"), 0)
        self.assertNotIn("DOCKER_CONFIG", self.recorded()[-1][1])

    def test_service_outside_the_project_runs_nothing(self):
        self.assertEqual(self.as_root("logs portainer"), 2)
        self.assertEqual(len(self.recorded()), 1)  # only `config --services`

    def test_unreadable_compose_file_refuses(self):
        open(os.path.join(self.dir, "broken"), "w").close()
        self.assertEqual(self.as_root("ps"), 2)
        self.assertEqual(len(self.recorded()), 1)

    def test_refuses_without_sudo_from_a_configured_user(self):
        for env in ({}, {"SUDO_USER": "dan"}, {"SUDO_USER": ""}):
            with self.subTest(env=env), \
                    mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(self.as_root("ps"), 2)
        self.assertEqual(self.recorded(), [])

    def test_refuses_what_the_unprivileged_half_would_refuse(self):
        for command in REFUSED:
            with self.subTest(command=command):
                self.assertEqual(self.as_root(command), 2)
        self.assertTrue(all(c[0][-2:] == ["config", "--services"]
                            for c in self.recorded()))

    def test_changes_take_turns(self):
        lock = os.path.join(self.dir, "compose-gate.minusbot.lock")
        with open(lock, "w") as held:
            # Even a shared hold must block a change: the tool's lock has to
            # be exclusive, or two changes could run together.
            fcntl.flock(held, fcntl.LOCK_SH)
            self.assertEqual(self.as_root("restart"), 75)
            self.assertEqual(self.as_root("ps"), 0)  # reads don't wait
        self.assertEqual(self.as_root("restart"), 0)


class Hangup(unittest.TestCase):
    """`logs --follow` stops when the client is gone, even if the
    containers are quiet: a real root process, a real closed pipe."""

    HARNESS = (
        "import json, os, sys\n"
        "sys.path.insert(0, %r)\n"
        "import compose_gate as gate\n"
        "gate.log = lambda message: None\n"
        "sys.exit(gate.as_root(json.loads(sys.argv[1]), sys.argv[2:]))\n"
    ) % os.path.dirname(os.path.abspath(gate.__file__))

    def test_follow_stops_when_the_client_hangs_up(self):
        tmp = tempfile.mkdtemp()
        pid_file = os.path.join(tmp, "pid")
        docker = os.path.join(tmp, "docker")
        with open(docker, "w") as f:
            f.write("#!%s\n" % sys.executable + (
                "import os, sys, time\n"
                "if sys.argv[-2:] == ['config', '--services']:\n"
                "    print('minusbot'); sys.exit()\n"
                "open(%r, 'w').write(str(os.getpid()))\n"
                "print('following', flush=True)\n"
                "time.sleep(60)  # a quiet container: no more output\n"
            ) % pid_file)
        os.chmod(docker, 0o755)
        root = subprocess.Popen(
            [sys.executable, "-c", self.HARNESS,
             json.dumps(config(docker=docker)), "logs", "--follow"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={"SUDO_USER": "illia", "PATH": os.environ.get("PATH", "")})
        self.addCleanup(lambda: root.poll() is None and root.kill())
        self.assertEqual(root.stdout.readline(), b"following\n")
        root.stdout.close()  # the client disconnects
        self.assertEqual(root.wait(timeout=10), 129)
        with open(pid_file) as f:
            docker_pid = int(f.read())
        for _ in range(50):
            try:
                os.kill(docker_pid, 0)
            except ProcessLookupError:
                return  # docker compose is gone too
            time.sleep(0.1)
        self.fail("docker compose still running after the client left")


class Main(unittest.TestCase):
    def test_missing_config_refuses(self):
        with mock.patch.object(gate, "CONFIG", "/nonexistent/config.json"), \
                mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(gate.main(), 2)


if __name__ == "__main__":
    unittest.main()
