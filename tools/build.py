#!/usr/bin/env python3
"""Build the selected test suites and produce the combined FAT image."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "suites" / "libctest" / "runner.py"
IMAGE = ROOT / "tools" / "image.py"


def run(command):
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run(command, check=True)


def isa_for(machine):
    if machine.startswith(("x86_64-", "amd64-")):
        return "x86_64"
    if machine.startswith("riscv64-"):
        return "rv64"
    if machine.startswith("riscv32-"):
        return "rv32"
    if machine.startswith("aarch64-"):
        return "aarch64"
    if machine.startswith("loongarch64-"):
        return "loongarch64"
    raise RuntimeError(f"unsupported compiler machine for image classification: {machine}")


def build(args):
    output_dir = args.out.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "tests.img").unlink(missing_ok=True)
    suites = (args.suite.split(",") if args.suite != "all" else
              ["libctest", "picolibc", "posixtst", "busybox"])
    unknown = sorted(set(suites) - {"libctest"})
    if unknown:
        reasons = {
            "picolibc": "no pinned Picolibc source or Meson test adapter",
            "posixtst": "optional LTP Open POSIX submodule and cross-build selection are not configured",
            "busybox": "no pinned BusyBox source or Linux testsuite adapter",
        }
        detail = "; ".join(f"{suite}: {reasons.get(suite, 'unknown suite')}" for suite in unknown)
        raise RuntimeError("suite not integrated: " + detail)
    targets = args.targets.split(",")
    if not targets or len(set(targets)) != len(targets):
        raise RuntimeError("TARGETS must be a comma-separated set of ISA names")
    artifact_root = output_dir / "artifacts"
    for suite in suites:
        for target in targets:
            if target == "x86_64":
                compiler = args.x86_64_cc
            elif target == "rv64":
                compiler = args.rv64_cc
            elif target == "rv32":
                compiler = args.rv32_cc
            elif target == "aarch64":
                compiler = args.aarch64_cc
            elif target == "loongarch64":
                compiler = args.loongarch64_cc
            else:
                raise RuntimeError(f"unsupported ISA target: {target}")
            temporary = output_dir / "compat" / target
            run([sys.executable, str(RUNNER), "build", "--target", "linux", "--cc", compiler,
                 "--out", str(temporary)])
            build_root = temporary / "linux"
            machine = json.loads((build_root / "build.json").read_text(encoding="utf-8"))["machine"]
            if isa_for(machine) != target:
                raise RuntimeError(f"{compiler} targets {machine}, expected {target}")
            suite_root = artifact_root / suite / target
            shutil.rmtree(suite_root, ignore_errors=True)
            suite_root.mkdir(parents=True, exist_ok=True)
            (suite_root / ".machine").write_text(machine + "\n", encoding="ascii")
            for binary in sorted((build_root / "bin").iterdir()):
                if not binary.is_file():
                    continue
                shutil.copy2(binary, suite_root / binary.name)
            if not any(suite_root.iterdir()):
                raise RuntimeError(f"{suite}/{target}: compiler produced no test programs")
    run([sys.executable, str(IMAGE), "pack", "--input", str(artifact_root),
         "--suites", ",".join(suites), "--output", str(output_dir / "tests.img")])
    run([sys.executable, str(IMAGE), "verify", "--input", str(artifact_root),
         "--suites", ",".join(suites), "--image", str(output_dir / "tests.img")])
    print(f"test image ready: {output_dir / 'tests.img'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="libctest", help="libctest or comma-separated names")
    parser.add_argument("--targets", default="rv64", help="comma-separated ISA targets")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--x86-64-cc", dest="x86_64_cc", default="gcc")
    parser.add_argument("--rv64-cc", default="riscv64-linux-gnu-gcc")
    parser.add_argument("--rv32-cc", default="riscv32-linux-gnu-gcc")
    parser.add_argument("--aarch64-cc", default="aarch64-linux-gnu-gcc")
    parser.add_argument("--loongarch64-cc", default="loongarch64-linux-gnu-gcc")
    args = parser.parse_args()
    try:
        build(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"test build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
