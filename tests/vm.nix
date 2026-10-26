# End to end: real sshd, sudo and docker. `client` holds the key and is the
# only allowed source; `stranger` has the same key and must be refused.
{ module }:
{ pkgs, lib, ... }:
let
  keys = pkgs.runCommand "compose-gate-test-keys" { nativeBuildInputs = [ pkgs.openssh ]; } ''
    mkdir $out
    ssh-keygen -q -t ed25519 -N "" -C test -f $out/key
  '';
  image = pkgs.dockerTools.buildImage {
    name = "gate-test";
    tag = "latest";
    copyToRoot = [ pkgs.busybox ];
    config.Cmd = [ "/bin/sh" "-c" "while true; do echo tick; sleep 1; done" ];
  };
  client = { pkgs, ... }: {
    environment.systemPackages = [ pkgs.openssh ];
  };
in
{
  name = "compose-gate";

  nodes = {
    inherit client;
    stranger = client;
    server = { nodes, ... }: {
      imports = [ module ];
      virtualisation.docker.enable = true;
      virtualisation.memorySize = 2048;
      services.openssh.enable = true;
      environment.etc."app/docker-compose.yml".text = ''
        services:
          web:
            image: gate-test:latest
            pull_policy: never
          sidecar:
            image: gate-test:latest
            pull_policy: never
      '';
      services.compose-gate = {
        enable = true;
        users.alice = {
          project = "app";
          directory = "/etc/app";
          authorizedKeys = [ (lib.fileContents "${keys}/key.pub") ];
          # Both families: the test network resolves `server` to IPv6 first.
          from = [
            nodes.client.networking.primaryIPAddress
            nodes.client.networking.primaryIPv6Address
          ];
        };
      };
    };
  };

  testScript = { nodes, ... }: ''
    start_all()
    server.wait_for_unit("docker.service")
    server.wait_for_unit("sshd.service")
    server.succeed("docker load < ${image}")
    server.succeed("docker compose -p app --project-directory /etc/app -f /etc/app/docker-compose.yml up -d")

    for m in (client, stranger):
        m.succeed("install -m 600 ${keys}/key /root/key")
    ssh = "ssh -i /root/key -o StrictHostKeyChecking=no -o BatchMode=yes alice@server"

    def gate(command):
        return client.succeed(f"{ssh} {command} 2>&1")

    def refused(command):
        status, out = client.execute(f"{ssh} {command} 2>&1")
        assert status != 0, f"{command!r} was accepted: {out}"
        return out

    with subtest("the account"):
        server.succeed("getent passwd alice | grep -q ':/var/empty:/run/current-system/sw/bin/compose-gate$'")
        server.fail("id -nG alice | grep -qw docker")
        out = server.succeed("sudo -l -U alice")
        assert "NOPASSWD: /run/current-system/sw/bin/compose-gate" in out, out
        server.fail("sudo -u alice sudo -n /bin/sh -c id")

    with subtest("allowed commands"):
        assert "app-web-1" in gate("ps"), "ps"
        client.wait_until_succeeds(f"{ssh} logs --tail 3 web 2>&1 | grep -q tick")
        gate("restart sidecar")
        gate("up --detach --wait")
        # No network in the VM: what docker does with the pull doesn't
        # matter here, only that the gate passed it on.
        _, out = client.execute(f"{ssh} pull --quiet 2>&1")
        assert "refused" not in out, out
        server.succeed("journalctl -t compose-gate | grep -q 'alice running pull --quiet'")
        assert "Options only in this order" in gate("help")

    with subtest("refused commands run nothing"):
        for command in ["id", "'logs; id'", "'ps && touch /tmp/pwned'",
                        "exec web sh", "run web sh", "-- -f /etc/passwd ps",
                        "logs --tail=5", "'logs --tail 5 --follow'",
                        "logs portainer", "'cp web:/bin/sh /tmp/pwned'"]:
            assert "refused" in refused(command), command
        server.fail("test -e /tmp/pwned")
        server.succeed("journalctl -t compose-gate | grep -q refused")

    with subtest("no pty, forwarding, sftp or scp"):
        # What the real sshd applies to alice from the client's address. (How
        # a client reacts to a refused pty differs: `-tt` makes recent
        # OpenSSH close the session, so test the server, not the client.)
        effective = server.succeed(
            "sshd -T -C user=alice,host=client,addr=${nodes.client.networking.primaryIPv6Address}")
        for line in ["permittty no", "disableforwarding yes", "permituserrc no",
                     "permittunnel no", "authenticationmethods publickey",
                     "forcecommand /run/current-system/sw/bin/compose-gate"]:
            # OpenSSH 10 prints names in CamelCase (PermitTTY), older ones in
            # lower case.
            assert line in effective.lower().splitlines(), f"sshd -T lacks {line!r}"
        # A local forward is refused only when something connects through it:
        # open it, then expect no SSH banner from zen's own port 22 through it.
        client.succeed(f"{ssh} -f -N -L 9999:localhost:22")
        client.fail("timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/9999; head -c4 <&3' | grep -q SSH-")
        client.succeed("pkill -f '[L] 9999:localhost:22'")  # [L]: not pkill's own shell
        client.fail(f"{ssh} -o ExitOnForwardFailure=yes -R 9999:localhost:22 ps")
        client.fail("sftp -i /root/key -o StrictHostKeyChecking=no -b /dev/null alice@server")
        client.fail("scp -O -i /root/key -o StrictHostKeyChecking=no /etc/hosts alice@server:/tmp/pwned")
        server.fail("test -e /tmp/pwned")

    with subtest("the key works only from the allowed address"):
        stranger.succeed("install -m 600 ${keys}/key /root/key")
        stranger.fail(f"{ssh} ps")
  '';
}
