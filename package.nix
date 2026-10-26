# The tool, with its unit tests and flake8 run in the build: any system that
# builds compose-gate runs them.
{ lib, stdenvNoCC, python3 }:
stdenvNoCC.mkDerivation {
  pname = "compose-gate";
  version = "0.1.0";
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./compose_gate.py ./test_compose_gate.py ];
  };

  nativeCheckInputs = [ python3 python3.pkgs.flake8 ];
  dontConfigure = true;
  dontBuild = true;
  doCheck = true;
  checkPhase = ''
    runHook preCheck
    flake8 compose_gate.py test_compose_gate.py
    python3 -m unittest -v
    runHook postCheck
  '';

  # -I: the root half ignores PYTHON* variables and the user's site dir
  # (sudo resets the environment anyway).
  installPhase = ''
    runHook preInstall
    install -Dm755 compose_gate.py $out/bin/compose-gate
    substituteInPlace $out/bin/compose-gate \
      --replace-fail '#!/usr/bin/env python3' '#!${python3}/bin/python3 -I'
    runHook postInstall
  '';

  meta = {
    description = "SSH forced command: a strict subset of docker compose for one project per user";
    license = lib.licenses.mit;
    mainProgram = "compose-gate";
    platforms = lib.platforms.linux;
  };
}
