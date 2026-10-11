#!/usr/bin/env python3
"""Build selected upstream test suites and assemble one disk image."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "suites" / "libctest" / "runner.py"
IMAGE = ROOT / "tools" / "image.py"
BUSYBOX_APPLETS = ["cat", "echo", "cp", "ls", "rm", "pwd", "printf"]
PICO_OPTIONS = [
    "-Dtests=true", "-Dnative-tests=false", "-Dtests-enable-posix-io=false",
    "-Dsemihost=false", "-Dos-linux=true", "-Dmultilib=false", "-Dtest-stdin=false",
]


def run(command, cwd=None, env=None, output=None):
    print("+", " ".join(map(str, command)), flush=True)
    if output is None:
        subprocess.run(command, check=True, cwd=cwd, env=env)
    else:
        with output.open("wb") as log:
            subprocess.run(command, check=True, cwd=cwd, env=env,
                           stdout=log, stderr=subprocess.STDOUT)


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


def elf_executable(path):
    try:
        header = path.read_bytes()[:20]
    except OSError:
        return False
    return len(header) >= 18 and header[:4] == b"\x7fELF" and header[16:18] == b"\x02\x00"


def build_libctest(target, artifact_root, args, output_dir):
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
    suite_root = artifact_root / "libctest" / target
    suite_root.mkdir(parents=True, exist_ok=True)
    (suite_root / ".machine").write_text(machine + "\n", encoding="ascii")
    build_data = json.loads((build_root / "build.json").read_text(encoding="utf-8"))
    runlist = []
    for binary in sorted((build_root / "bin").iterdir()):
        if binary.is_file():
            shutil.copy2(binary, suite_root / binary.name)
    for case in build_data["tests"]:
        runlist.append({"name": case["name"], "source": f"libctest/{target}/{Path(case.get('binary', '')).name}",
                        "status": case["status"], "reason": case.get("reason", ""),
                        "timeout": 30})
    (suite_root / ".runlist.json").write_text(json.dumps(runlist, indent=2) + "\n",
                                               encoding="utf-8")


def meson_passes(log):
    passed = set()
    chunks = re.split(r"(?m)^=+\s+\d+/\d+\s+=+$", log)
    for chunk in chunks:
        test = re.search(r"(?m)^test:\s*(.*?)\s*/\s*(.*?)\s*$", chunk)
        result = re.search(r"(?m)^result:\s*(.*?)\s*$", chunk)
        if test and result and result.group(1) == "exit status 0":
            passed.add((test.group(1), test.group(2)))
    return passed


def build_picolibc(target, output_dir, artifact_root, args):
    if target != "x86_64":
        raise RuntimeError("Picolibc Linux reference suite currently targets x86_64 only")
    source = ROOT / "third_party" / "picolibc"
    cross_file = source / "scripts" / "cross-x86_64-linux-gnu.txt"
    if not (source / "meson.build").is_file():
        raise RuntimeError("initialize third_party/picolibc in KaleidOS-Tests first")
    if shutil.which("meson") is None:
        raise RuntimeError("Picolibc requires Meson (Debian/Ubuntu: apt install meson ninja-build)")
    if shutil.which("ninja") is None:
        raise RuntimeError("Picolibc requires Ninja (Debian/Ubuntu: apt install ninja-build)")
    build_dir = output_dir / "picolibc-build" / target
    shutil.rmtree(build_dir, ignore_errors=True)
    run(["meson", "setup", str(build_dir), str(source), "--cross-file", str(cross_file),
         *PICO_OPTIONS])
    run(["meson", "compile", "-C", str(build_dir), f"-j{args.jobs}"],
        output=output_dir / "picolibc-build.log")
    run(["meson", "test", "-C", str(build_dir), "--print-errorlogs",
         "--num-processes", str(args.jobs)], output=output_dir / "picolibc-tests.log")

    passed = meson_passes((build_dir / "meson-logs" / "testlog.txt").read_text(encoding="utf-8"))
    targets = json.loads(subprocess.check_output(
        ["meson", "introspect", "--targets", str(build_dir)], text=True))
    suite_root = artifact_root / "picolibc" / target
    shutil.rmtree(suite_root, ignore_errors=True)
    suite_root.mkdir(parents=True)
    machine = subprocess.check_output([args.x86_64_cc, "-dumpmachine"], text=True).strip()
    (suite_root / ".machine").write_text(machine + "\n", encoding="ascii")
    copied = {}
    for target_info in targets:
        if target_info.get("type") != "executable":
            continue
        defined_in = Path(target_info.get("defined_in", ""))
        if "test" not in defined_in.parts:
            continue
        for filename in target_info.get("filename", []):
            binary = Path(filename)
            if not elf_executable(binary):
                continue
            relative = binary.relative_to(build_dir).as_posix()
            artifact_name = re.sub(r"[^A-Za-z0-9_.-]", "_", relative.replace("/", "_"))
            destination = suite_root / artifact_name
            shutil.copy2(binary, destination)
            copied[relative] = artifact_name

    runlist = []
    for test in json.loads((build_dir / "meson-info" / "intro-tests.json").read_text(encoding="utf-8")):
        command = test.get("cmd", [])
        if not command:
            continue
        binary = Path(command[0])
        try:
            relative = binary.relative_to(build_dir).as_posix()
        except ValueError:
            continue
        if relative not in copied or (test.get("suite", [""])[0], test["name"]) not in passed:
            continue
        runlist.append({"source": f"picolibc/{target}/{copied[relative]}",
                        "name": f"{test.get('suite', ['picolibc'])[0]} / {test['name']}",
                        "args": command[1:], "env": test.get("env", {}),
                        "timeout": test.get("timeout", 30)})
    (suite_root / ".runlist.json").write_text(json.dumps(runlist, indent=2) + "\n",
                                               encoding="utf-8")
    if not runlist:
        raise RuntimeError("Picolibc built, but Meson reported no runnable passing tests")


def build_busybox(target, output_dir, artifact_root, args):
    if target != "x86_64":
        raise RuntimeError("BusyBox Linux reference tests currently target x86_64 only")
    source = ROOT / "third_party" / "busybox"
    if not (source / "Makefile").is_file():
        raise RuntimeError("initialize third_party/busybox in KaleidOS-Tests first")
    build_dir = output_dir / "busybox-build" / target
    shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    run(["make", f"O={build_dir}", "defconfig"], cwd=source,
        output=output_dir / "busybox-defconfig.log")
    config = build_dir / ".config"
    contents = config.read_text(encoding="utf-8")
    if "CONFIG_TC=y" not in contents:
        raise RuntimeError("BusyBox defconfig did not enable the tc applet as expected")
    config.write_text(contents.replace("CONFIG_TC=y", "# CONFIG_TC is not set", 1),
                       encoding="utf-8")
    run(["make", f"O={build_dir}", "silentoldconfig"], cwd=source)
    run(["make", f"O={build_dir}", f"-j{args.jobs}"], cwd=source,
        output=output_dir / "busybox-build.log")
    binary = build_dir / "busybox"
    if not binary.is_file():
        raise RuntimeError("BusyBox build completed without a busybox executable")
    machine = subprocess.check_output([args.x86_64_cc, "-dumpmachine"], text=True).strip()
    if isa_for(machine) != target:
        raise RuntimeError(f"{args.x86_64_cc} targets {machine}, expected {target}")

    suite_root = artifact_root / "busybox" / target
    shutil.rmtree(suite_root, ignore_errors=True)
    suite_root.mkdir(parents=True)
    (suite_root / ".machine").write_text(machine + "\n", encoding="ascii")
    shutil.copy2(binary, suite_root / "busybox")
    shutil.copy2(config, suite_root / "bbconfig.data")

    test_dir = build_dir / "testsuite"
    shutil.copytree(source / "testsuite", test_dir)
    for entry in source.iterdir():
        if entry.name in {"busybox", "testsuite"}:
            continue
        for parent in (build_dir, test_dir):
            destination = parent / entry.name
            if not destination.exists():
                destination.symlink_to(entry, target_is_directory=entry.is_dir())
    (test_dir / "busybox").symlink_to(binary)
    echo_helper = test_dir / "echo-ne"
    run([args.x86_64_cc, "-Os", "-o", str(echo_helper), str(source / "scripts" / "echo.c")])
    environment = {
        "PATH": f"{build_dir}:{Path(shutil.which('sh')).parent}:" + os.environ.get("PATH", ""),
        "tsdir": str(test_dir), "bindir": str(build_dir), "SKIP_INTERNET_TESTS": "1",
        "ECHO": str(echo_helper),
    }
    test_log = output_dir / "busybox-testsuite.log"
    run(["sh", str(test_dir / "runtest"), *BUSYBOX_APPLETS], cwd=test_dir,
        env={**os.environ, **environment}, output=test_log)
    with tarfile.open(suite_root / "bbtests.data", "w:gz") as archive:
        archive.add(source / "testsuite", arcname="testsuite")
    shutil.copy2(echo_helper, suite_root / "bbecho")
    (suite_root / ".runlist.json").write_text(json.dumps({
        "applets": BUSYBOX_APPLETS,
        "binary": f"busybox/{target}/busybox",
        "config": f"busybox/{target}/bbconfig.data",
        "tests": f"busybox/{target}/bbtests.data",
        "echo": f"busybox/{target}/bbecho",
    }, indent=2) + "\n", encoding="utf-8")


def build(args):
    output_dir = args.out.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "tests.img").unlink(missing_ok=True)
    suites = args.suite.split(",") if args.suite != "all" else ["libctest", "picolibc", "busybox"]
    if not suites or len(set(suites)) != len(suites):
        raise RuntimeError("SUITE must be a non-empty, duplicate-free list")
    if "ltp" in suites:
        raise RuntimeError("the full LTP suite is deferred from the initial host workflow")
    if set(suites) & {"posixtst", "openposix"}:
        raise RuntimeError("Open POSIX has its own upstream suite under LTP; its adapter is not implemented yet")
    unknown = sorted(set(suites) - {"libctest", "picolibc", "busybox"})
    if unknown:
        raise RuntimeError("unknown suites: " + ", ".join(unknown))
    targets = args.targets.split(",")
    if not targets or len(set(targets)) != len(targets):
        raise RuntimeError("TARGETS must be a comma-separated set of ISA names")
    if args.fs not in ("fat", "ext4"):
        raise RuntimeError("FS must be fat or ext4")
    artifact_root = output_dir / "artifacts"
    shutil.rmtree(artifact_root, ignore_errors=True)
    for suite in suites:
        for target in targets:
            if suite == "libctest":
                build_libctest(target, artifact_root, args, output_dir)
            elif suite == "picolibc":
                build_picolibc(target, output_dir, artifact_root, args)
            else:
                build_busybox(target, output_dir, artifact_root, args)
    run([sys.executable, str(IMAGE), "pack", "--input", str(artifact_root),
         "--suites", ",".join(suites), "--fs", args.fs,
         "--output", str(output_dir / "tests.img")])
    run([sys.executable, str(IMAGE), "verify", "--input", str(artifact_root),
         "--suites", ",".join(suites), "--fs", args.fs,
         "--image", str(output_dir / "tests.img")])
    print(f"test image ready: {output_dir / 'tests.img'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="all", help="all or comma-separated suite names")
    parser.add_argument("--targets", default="x86_64", help="comma-separated ISA names")
    parser.add_argument("--fs", default="fat", help="disk filesystem: fat or ext4")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--x86-64-cc", dest="x86_64_cc", default="gcc")
    parser.add_argument("--rv64-cc", default="riscv64-linux-gnu-gcc")
    parser.add_argument("--rv32-cc", default="riscv32-linux-gnu-gcc")
    parser.add_argument("--aarch64-cc", default="aarch64-linux-gnu-gcc")
    parser.add_argument("--loongarch64-cc", default="loongarch64-linux-gnu-gcc")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    try:
        if args.jobs < 1:
            raise RuntimeError("--jobs must be positive")
        build(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"test build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
