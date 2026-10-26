# services.compose-gate: SSH accounts that can run only a strict subset of
# `docker compose` on one project each (see compose_gate.py).
#
# For each user it creates a system account whose login shell and sshd
# ForceCommand are compose-gate, with no password, no home, no pty and no
# forwarding; one sudo rule that lets these users run compose-gate (and
# nothing else) as root; and /etc/compose-gate/config.json, which maps each
# user to their project.
{ config, lib, ... }:
let
  inherit (lib) mkOption types;
  cfg = config.services.compose-gate;
  # One path for the login shell, sshd and sudo; sudo matches it exactly.
  bin = "/run/current-system/sw/bin/compose-gate";
  names = lib.attrNames cfg.users;

  userOptions = {
    options = {
      project = mkOption {
        type = types.strMatching "[a-z0-9][a-z0-9_-]*";
        description = "Compose project name.";
      };
      directory = mkOption {
        type = types.strMatching "/.*";
        description = "The project directory. Keep it and the compose file writable only by root: whoever can change the file controls what `up` runs.";
      };
      file = mkOption {
        type = types.str;
        default = "docker-compose.yml";
        description = "Compose file, relative to `directory`.";
      };
      dockerConfig = mkOption {
        type = types.nullOr (types.strMatching "/.*");
        default = null;
        description = "DOCKER_CONFIG for this project (registry logins for `pull`).";
      };
      authorizedKeys = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "Public keys, without options: `restrict` (and `from`) are added.";
      };
      from = mkOption {
        type = types.listOf types.str;
        default = [ ];
        description = "If not empty, the keys work only from these addresses or patterns (sshd `from=`).";
      };
    };
  };

  keyLine = u: key:
    "restrict"
    + lib.optionalString (u.from != [ ]) '',from="${lib.concatStringsSep "," u.from}"''
    + " " + key;
in
{
  options.services.compose-gate = {
    enable = lib.mkEnableOption "compose-gate SSH accounts";
    package = mkOption {
      type = types.package;
      description = "The compose-gate package.";
    };
    users = mkOption {
      type = types.attrsOf (types.submodule userOptions);
      default = { };
      description = "One entry per SSH account: the project it may operate.";
    };
  };

  config = lib.mkIf (cfg.enable && cfg.users != { }) {
    assertions = [
      {
        assertion = config.virtualisation.docker.enable;
        message = "services.compose-gate needs virtualisation.docker.enable";
      }
      {
        assertion = lib.all (n: !(builtins.elem n [ "root" ])) names;
        message = "services.compose-gate: root can't be a gate user";
      }
    ];

    environment.systemPackages = [ cfg.package ];

    environment.etc."compose-gate/config.json".text = builtins.toJSON {
      sudo = "${config.security.wrapperDir}/sudo";
      self = bin;
      docker = "${config.virtualisation.docker.package}/bin/docker";
      path = "/run/current-system/sw/bin";
      users = lib.mapAttrs
        (_: u: {
          inherit (u) project directory file;
          docker_config = u.dockerConfig;
        })
        cfg.users;
    };

    users.groups = lib.mapAttrs (_: _: { }) cfg.users;
    users.users = lib.mapAttrs
      (name: u: {
        isSystemUser = true;
        group = name;
        description = "compose-gate, project ${u.project}";
        home = "/var/empty";
        createHome = false;
        shell = bin;
        # No password, but not "!": sshd treats a "!" password as a locked
        # account and would refuse the key.
        hashedPassword = "*";
        openssh.authorizedKeys.keys = map (keyLine u) u.authorizedKeys;
      })
      cfg.users;

    # Root for exactly this program; it checks its arguments itself.
    security.sudo.extraRules = [
      {
        users = names;
        runAs = "root";
        commands = [ { command = bin; options = [ "NOPASSWD" ]; } ];
      }
    ];

    # Whatever the client asks for (a command, sftp, a pty, a forward), sshd
    # runs compose-gate with no pty and no forwarding. `Match all` closes the
    # block, so sshd options appended after it stay global.
    services.openssh.extraConfig = ''
      Match User ${lib.concatStringsSep "," names}
        ForceCommand ${bin}
        AuthenticationMethods publickey
        DisableForwarding yes
        PermitTTY no
        PermitUserRC no
        PermitTunnel no
      Match all
    '';
  };
}
