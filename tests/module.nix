# The module's generated config, without a VM: the account, sshd's Match
# block (and that it is closed), the sudo rule and config.json. Runs anywhere
# `nix flake check` runs; tests/vm.nix covers the behaviour.
{ nixpkgs, module, pkgs }:
let
  system = nixpkgs.lib.nixosSystem {
    modules = [
      module
      {
        nixpkgs.hostPlatform = pkgs.stdenv.hostPlatform.system;
        boot.isContainer = true;
        system.stateVersion = "26.05";
        virtualisation.docker.enable = true;
        services.openssh.enable = true;
        services.compose-gate = {
          enable = true;
          users.alice = {
            project = "app";
            directory = "/srv/app";
            dockerConfig = "/root/.docker";
            authorizedKeys = [ "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOk7eTcPDl4dB4fN0bAHMDqgcMbzcFU9ad0Tbhb2fnmX test" ];
            from = [ "192.0.2.10" ];
          };
        };
      }
    ];
  };
  inherit (system) config;
  alice = config.users.users.alice;
  etc = config.environment.etc;
  bin = "/run/current-system/sw/bin/compose-gate";
in
assert alice.shell == bin;
assert alice.home == "/var/empty";
assert !(builtins.elem "docker" alice.extraGroups);
assert alice.openssh.authorizedKeys.keys == [
  ''restrict,from="192.0.2.10" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOk7eTcPDl4dB4fN0bAHMDqgcMbzcFU9ad0Tbhb2fnmX test''
];
pkgs.runCommand "compose-gate-module-check" { nativeBuildInputs = [ pkgs.python3 ]; } ''
  sshd=${etc."ssh/sshd_config".source}
  grep -A7 '^Match User alice$' $sshd > block
  for line in "ForceCommand ${bin}" "AuthenticationMethods publickey" \
      "DisableForwarding yes" "PermitTTY no" "PermitUserRC no" "PermitTunnel no"; do
    grep -qx "  $line" block || { echo "sshd_config: missing '$line'"; cat block; exit 1; }
  done
  grep -qx 'Match all' block || { echo "sshd_config: Match block not closed"; exit 1; }

  grep -qxE 'alice[[:space:]]+ALL=\(root\)[[:space:]]+NOPASSWD: ${bin}' ${etc.sudoers.source} \
    || { echo "sudoers: rule missing"; grep alice ${etc.sudoers.source}; exit 1; }

  python3 - ${etc."compose-gate/config.json".source} <<'PY'
  import json, sys
  c = json.load(open(sys.argv[1]))
  assert c["self"] == "${bin}", c
  assert c["sudo"].endswith("/sudo") and c["docker"].endswith("/bin/docker"), c
  assert c["users"] == {"alice": {"project": "app", "directory": "/srv/app",
      "file": "docker-compose.yml", "docker_config": "/root/.docker"}}, c
  PY
  touch $out
''
