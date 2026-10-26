# compose-gate

Let someone roll out new images of *their* Docker Compose project on your
host, over SSH, without giving them Docker. Access to the Docker socket (the
`docker` group, Portainer, Dockge and the like) is root on the host.
compose-gate gives an SSH account a short list of real `docker compose`
commands on one project, and nothing else.

```sh
ssh alice@host pull
ssh alice@host up --detach --wait
ssh alice@host logs --since 30m --tail 100 web
```

## Commands

Each command is spelled exactly one way. Options must come in this order,
long names only, with a space before a value (never `=`). Services come last.

| Command | Options, in this order |
|---|---|
| `ps [SERVICE...]` | none |
| `logs [...] [SERVICE...]` | `[--follow] [--since D] [--tail N] [--timestamps]` (D like `45s`, `30m`, `2h`; N 1–9999; `--follow` ends after 1 hour) |
| `pull [...] [SERVICE...]` | `[--quiet]` |
| `up ... [SERVICE...]` | `--detach [--force-recreate] --wait` (`--detach` and `--wait` required) |
| `restart [SERVICE...]` | none |
| `help` | prints this table |

Anything else is refused. That includes every global option (`-f`,
`-p`, `--project-directory`, `--env-file`) and every other subcommand
(`exec`, `run`, `cp`, `config`, `build`, `down`, …), as well as shell syntax,
a pty, port forwarding, sftp and scp.

## How it works

- **Exact spelling, so nothing is interpreted.** Every accepted command means
  one thing. Its words go to `docker compose` unchanged, after the project,
  compose file and directory from the host's configuration. The gate can't
  read a command differently from docker, because there's only one way to
  write each one.
- **Two halves, one file** (`compose_gate.py`):
  - **As the SSH user** (its login shell and sshd's `ForceCommand`), it checks
    `SSH_ORIGINAL_COMMAND` and re-runs itself with `sudo -n`, passing the same
    words.
  - **As root,** it checks them again, without trusting the first half. This
    time service names are checked against `docker compose config --services`
    of the configured file. Then it runs docker, with a fixed environment and
    no shell.
- **Logging and limits:**
  - every request, accepted or refused, goes to the journal (`journalctl -t compose-gate`);
  - `pull`, `up` and `restart` take a per-project lock;
  - every command has a timeout;
  - `logs` stops as soon as the client disconnects, even when the containers
    are quiet (without a pty, sshd doesn't signal the command).

## NixOS

```nix
{
  inputs.compose-gate.url = "github:DanielHreben/compose-gate";
  inputs.compose-gate.inputs.nixpkgs.follows = "nixpkgs";

  # in the host's modules:
  imports = [ inputs.compose-gate.nixosModules.default ];
  services.compose-gate = {
    enable = true;
    users.alice = {
      project = "app";                 # compose project name
      directory = "/srv/app";          # holds docker-compose.yml
      dockerConfig = "/root/.docker";  # optional: registry logins for pull
      authorizedKeys = [ "ssh-ed25519 AAAA... alice" ];
      from = [ "192.0.2.10" ];         # optional: sshd from= restriction
    };
  };
}
```

For each user, the module creates:
- a system account with no password, home `/var/empty`, and compose-gate as
  its shell;
- an sshd `Match` block: `ForceCommand`, no pty, no forwarding, keys only;
- key lines with `restrict` (and `from=` if set);
- one sudo rule that lets these users run compose-gate as root;
- `/etc/compose-gate/config.json`.

It needs `virtualisation.docker.enable`.

## What it does not protect against

- **Whoever can write the compose file or the project directory controls what
  `up` runs, so they are root.** Keep both writable by root only. On NixOS,
  install them from the configuration.
- **The images.** `pull` and `up` run whatever the image's publisher pushed,
  inside its container. Limit the container itself: memory, pids,
  `cap_drop`, `no-new-privileges`, and its network.
- **Volume contents** are the container's business.

## Tests

- `python3 -m unittest -v` covers the command table, 60 refused commands,
  both halves against a fake `sudo` and `docker` (argv, environment,
  refusals and the lock), and `logs --follow` stopping when the client
  hangs up. The package build runs these and flake8, so
  every host that builds compose-gate runs them.
- `tests/module.nix` evaluates a NixOS system using the module and checks
  the generated account, `sshd_config` block, sudo rule and `config.json`.
  It needs no VM.
- `nix flake check` also runs `tests/vm.nix`, a NixOS VM test with real
  sshd, sudo and docker. It needs KVM: under software emulation, Docker
  inside the VM is too slow to start. It checks the account, the allowed commands,
  refusals that run nothing, no pty, forwarding, sftp or scp, and the key
  being refused from another address. CI runs it on every push.
