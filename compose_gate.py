#!/usr/bin/env python3
"""compose-gate: an SSH user's only command. A small subset of
`docker compose`, on the one Compose project configured for that user.

    ssh <user>@<host> <command>

Each command is spelled exactly one way: options in the order `help` shows,
long names only, a space between an option and its value, services last.
Every accepted command therefore means one thing, and its words go to
`docker compose` exactly as typed, after the project, file and directory
from the configuration. Anything else is refused.

Two halves in one file. As the SSH user (login shell and sshd's
ForceCommand) it checks SSH_ORIGINAL_COMMAND and re-runs itself through
`sudo -n` with the same words. As root it checks them again, this time
against the services in the project's compose file, and runs docker.
Nothing reaches a shell.

The configuration (CONFIG, written by the NixOS module) is JSON:

    {"sudo": "/run/wrappers/bin/sudo",
     "self": "/run/current-system/sw/bin/compose-gate",
     "docker": "/nix/store/...-docker/bin/docker",
     "path": "/run/current-system/sw/bin",
     "users": {"alice": {"project": "app",
                         "directory": "/srv/app",
                         "file": "docker-compose.yml",
                         "docker_config": "/root/.docker"}}}

`docker_config` is optional (registry logins for `pull`).
"""
import fcntl
import json
import os
import pwd
import re
import select
import shlex
import subprocess
import sys
import syslog
import time

CONFIG = "/etc/compose-gate/config.json"
LOCK_DIR = "/run"
MAX_COMMAND = 200
FOLLOW_SECONDS = 3600
WORD = re.compile(r"[A-Za-z0-9_.-]+")
# A service name as Compose allows it. Never starts with "-", so an option
# in the wrong place can't pass for a service.
SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
DURATION = re.compile(r"[1-9][0-9]{0,3}[smh]")
LINES = re.compile(r"[1-9][0-9]{0,3}")

# The allowed subcommands. Each lists its options in the only order they are
# accepted: (option, value pattern or None for a plain switch, required).
# Every subcommand takes services after its options. This table is the
# security policy: extend it only with options that can't name a file, a
# command, another project or a privilege.
COMMANDS = {
    "ps": [],
    "logs": [
        ("--follow", None, False),
        ("--since", DURATION, False),
        ("--tail", LINES, False),
        ("--timestamps", None, False),
    ],
    "pull": [("--quiet", None, False)],
    "up": [
        ("--detach", None, True),
        ("--force-recreate", None, False),
        ("--wait", None, True),
    ],
    "restart": [],
}
TIMEOUTS = {"ps": 60, "logs": 120, "pull": 900, "up": 300, "restart": 300}
CHANGES = ("pull", "up", "restart")  # one at a time per project (the lock)

USAGE = """\
ssh <you>@<this host> <command>. Options only in this order, services last:
  ps [SERVICE...]
  logs [--follow] [--since D] [--tail N] [--timestamps] [SERVICE...]
  pull [--quiet] [SERVICE...]
  up --detach [--force-recreate] --wait [SERVICE...]
  restart [SERVICE...]
  help
D: like 45s, 30m, 2h. N: 1-9999 lines. --follow stops after 1 hour.
SERVICE: a service of your project.
To roll out new images: pull, then up --detach --wait.
"""


class Refused(Exception):
    pass


def check(words, services=None):
    """Raise Refused unless `words` is an allowed command, spelled exactly.

    `services` is the project's service list (the root half reads it from
    the compose file). The unprivileged half can't run docker and passes
    None: it checks only that a name looks like one, and the root half
    checks it is really in the file."""
    if not words or words == ["help"]:
        return
    for word in words:
        if not WORD.fullmatch(word):
            raise Refused("unexpected characters")
    command, rest = words[0], words[1:]
    if command not in COMMANDS:
        raise Refused("unknown command " + command)
    i = 0
    for option, value, required in COMMANDS[command]:
        if i < len(rest) and rest[i] == option:
            i += 1
            if value is not None:
                if i >= len(rest) or not value.fullmatch(rest[i]):
                    raise Refused("bad value for " + option)
                i += 1
        elif required:
            raise Refused("%s needs %s" % (command, option))
    names = rest[i:]
    for name in names:
        if not SERVICE.fullmatch(name):
            raise Refused("option out of order or unknown: " + name)
        if services is not None and name not in services:
            raise Refused("not a service of this project: " + name)
    if len(set(names)) != len(names):
        raise Refused("repeated service")


def load_config():
    with open(CONFIG) as f:
        return json.load(f)


def log(message):
    syslog.openlog("compose-gate", 0, syslog.LOG_AUTH)
    syslog.syslog(syslog.LOG_NOTICE, message)


def client():
    return (os.environ.get("SSH_CONNECTION") or "?").split(" ")[0]


def as_user(config):
    """Unprivileged half. Ignores its own argv: sshd runs the login shell
    as `compose-gate -c <ForceCommand>`; only SSH_ORIGINAL_COMMAND counts."""
    user = pwd.getpwuid(os.getuid()).pw_name
    raw = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        if user not in config["users"]:
            raise Refused("no project for user " + user)
        if len(raw) > MAX_COMMAND:
            raise Refused("command too long")
        try:
            words = shlex.split(raw)
        except ValueError:
            raise Refused("unbalanced quotes")
        check(words)
    except Refused as why:
        log("%s refused from %s: %r (%s)"
            % (user, client(), raw[:MAX_COMMAND], why))
        print("refused: %s\n\n%s" % (why, USAGE), file=sys.stderr, end="")
        return 2
    if not words or words == ["help"]:
        print(USAGE, end="")
        return 0
    log("%s request from %s: %s" % (user, client(), " ".join(words)))
    sys.stdout.flush()
    sudo = config["sudo"]
    os.execv(sudo, [sudo, "-n", config["self"]] + words)


class Project:
    """The configured project of one user, run with a fixed environment."""

    def __init__(self, config, user):
        entry = config["users"][user]
        self.name = entry["project"]
        self.command = [
            config["docker"], "compose",
            "--project-name", self.name,
            "--project-directory", entry["directory"],
            "--file", os.path.join(entry["directory"], entry["file"]),
        ]
        self.env = {"PATH": config["path"], "HOME": "/root"}
        if entry.get("docker_config"):
            self.env["DOCKER_CONFIG"] = entry["docker_config"]

    def services(self):
        """The services in the project's compose file, which only root
        (the host's configuration) can change."""
        try:
            out = subprocess.run(
                self.command + ["config", "--services"], env=self.env,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=60, check=True,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            raise Refused("can't read the project's services")
        return set(out.split())

    def run(self, args, timeout, stop_on_hangup=False):
        """Run `docker compose <args>`, stopped after `timeout` seconds.

        With `stop_on_hangup` (read-only commands) it also stops as soon as
        the client is gone. Without a pty, sshd doesn't signal the command
        when the connection drops; it only closes the command's output, and
        a quiet `logs --follow` wouldn't notice until its next write."""
        if not stop_on_hangup:
            try:
                return subprocess.run(
                    self.command + args, env=self.env,
                    stdin=subprocess.DEVNULL, timeout=timeout,
                ).returncode
            except subprocess.TimeoutExpired:
                print("stopped after %d seconds" % timeout, file=sys.stderr)
                return 124
        proc = subprocess.Popen(
            self.command + args, env=self.env, stdin=subprocess.DEVNULL)
        # Event mask 0: only POLLERR/POLLHUP, which a pipe or socket reports
        # once its reader (sshd) has closed it.
        watch = select.poll()
        watch.register(sys.stdout.fileno(), 0)
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            left = deadline - time.monotonic()
            if left <= 0:
                stop(proc)
                print("stopped after %d seconds" % timeout, file=sys.stderr)
                return 124
            if watch.poll(min(left, 1.0) * 1000):
                stop(proc)
                return 129  # the client hung up
        return proc.returncode


def stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def as_root(config, argv):
    """Privileged half: trusts nothing the unprivileged half sent."""
    user = os.environ.get("SUDO_USER", "")
    if user not in config["users"]:
        print("refused: only for configured users via sudo", file=sys.stderr)
        return 2
    if not argv or argv == ["help"]:
        print(USAGE, end="")
        return 0
    project = Project(config, user)
    try:
        check(argv, project.services())
    except Refused as why:
        log("root half refused %s: %r (%s)" % (user, argv, why))
        print("refused: %s" % why, file=sys.stderr)
        return 2
    command = argv[0]
    timeout = TIMEOUTS[command]
    if command == "logs" and "--follow" in argv:
        timeout = FOLLOW_SECONDS
    if command not in CHANGES:
        return project.run(argv, timeout, stop_on_hangup=command == "logs")
    lock_path = os.path.join(LOCK_DIR, "compose-gate.%s.lock" % project.name)
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("busy: another pull, up or restart is running",
                  file=sys.stderr)
            return 75
        log("%s running %s" % (user, " ".join(argv)))
        return project.run(argv, timeout)


def main():
    try:
        config = load_config()
    except (OSError, ValueError):
        print("refused: compose-gate is not configured", file=sys.stderr)
        return 2
    if os.geteuid() == 0:
        return as_root(config, sys.argv[1:])
    return as_user(config)


if __name__ == "__main__":
    sys.exit(main())
