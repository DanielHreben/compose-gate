{
  description = "compose-gate: SSH accounts that can run only a strict subset of docker compose on one project each";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAll (pkgs: { default = pkgs.callPackage ./package.nix { }; });

      nixosModules.default = { pkgs, lib, ... }: {
        imports = [ ./module.nix ];
        services.compose-gate.package =
          lib.mkDefault self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      };

      checks = forAll (pkgs: {
        package = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        vm = pkgs.testers.runNixOSTest (import ./tests/vm.nix { module = self.nixosModules.default; });
      });

      formatter = forAll (pkgs: pkgs.nixpkgs-fmt);
    };
}
