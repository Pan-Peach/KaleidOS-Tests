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


def compiler_for(target, args):
    return {"x86_64": args.x86_64_cc, "rv64": args.rv64_cc, "rv32": args.rv32_cc,
            "aarch64": args.aarch64_cc, "loongarch64": args.loongarch64_cc}[target]


def abi_for(machine):
    if "-linux-musl" in machine:
        return "linux-musl"
    if "-linux" in machine:
        return "linux-gnu"
    raise RuntimeError(f"unsupported userspace ABI: {machine}")


def write_unsupported(root, suite, target, reason):
    suite_root = root / suite / target
    shutil.rmtree(suite_root, ignore_errors=True)
    suite_root.mkdir(parents=True)
    (suite_root / ".abi").write_text("linux-gnu\n", encoding="ascii")
    (suite_root / ".libc").write_text("unsupported\n", encoding="ascii")
    (suite_root / ".runlist.json").write_text(json.dumps([{
        "name": suite, "status": "UNSUPPORTED", "reason": reason,
    }], indent=2) + "\n", encoding="utf-8")


def write_build_failure(root, suite, target, error):
    suite_root = root / suite / target
    shutil.rmtree(suite_root, ignore_errors=True)
    suite_root.mkdir(parents=True)
    (suite_root / ".abi").write_text("linux-gnu\n", encoding="ascii")
    (suite_root / ".libc").write_text("unknown\n", encoding="ascii")
    (suite_root / ".runlist.json").write_text(json.dumps([{
        "name": suite, "status": "BUILD_FAIL", "reason": str(error),
    }], indent=2) + "\n", encoding="utf-8")


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
    (suite_root / ".abi").write_text(abi_for(machine) + "\n", encoding="ascii")
    (suite_root / ".libc").write_text("glibc\n" if "musl" not in machine else "musl\n",
                                       encoding="ascii")
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
        write_unsupported(artifact_root, "picolibc", target, "Picolibc adapter is x86_64 only")
        return
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
    (suite_root / ".abi").write_text("linux-gnu\n", encoding="ascii")
    (suite_root / ".libc").write_text("picolibc\n", encoding="ascii")
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
    compiler = compiler_for(target, args)
    cross = "" if target == "x86_64" else compiler.removesuffix("gcc")
    source = ROOT / "third_party" / "busybox"
    if not (source / "Makefile").is_file():
        raise RuntimeError("initialize third_party/busybox in KaleidOS-Tests first")
    build_dir = output_dir / "busybox-build" / target
    shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    prefix = [f"CROSS_COMPILE={cross}"] if cross else []
    run(["make", f"O={build_dir}", *prefix, "defconfig"], cwd=source,
        output=output_dir / "busybox-defconfig.log")
    config = build_dir / ".config"
    contents = config.read_text(encoding="utf-8")
    contents = re.sub(r"(?m)^# CONFIG_STATIC is not set$", "CONFIG_STATIC=y", contents)
    if target != "x86_64":
        contents = re.sub(r"(?m)^CONFIG_SHA1_HWACCEL=y$",
                          "# CONFIG_SHA1_HWACCEL is not set", contents)
    contents = re.sub(r"(?m)^CONFIG_TC=y$", "# CONFIG_TC is not set", contents)
    if "CONFIG_STATIC=y" not in contents:
        contents += "\nCONFIG_STATIC=y\n"
    config.write_text(contents, encoding="utf-8")
    run(["make", f"O={build_dir}", *prefix, "silentoldconfig"], cwd=source)
    run(["make", f"O={build_dir}", *prefix, f"-j{args.jobs}"], cwd=source,
        output=output_dir / "busybox-build.log")
    binary = build_dir / "busybox"
    if not binary.is_file():
        raise RuntimeError("BusyBox build completed without a busybox executable")
    machine = subprocess.check_output([compiler, "-dumpmachine"], text=True).strip()
    if isa_for(machine) != target:
        raise RuntimeError(f"{args.x86_64_cc} targets {machine}, expected {target}")

    suite_root = artifact_root / "busybox" / target
    shutil.rmtree(suite_root, ignore_errors=True)
    suite_root.mkdir(parents=True)
    (suite_root / ".machine").write_text(machine + "\n", encoding="ascii")
    (suite_root / ".abi").write_text(abi_for(machine) + "\n", encoding="ascii")
    (suite_root / ".libc").write_text("glibc\n" if "musl" not in machine else "musl\n",
                                       encoding="ascii")
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
    if target == "x86_64":
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


def build_openposix(target, output_dir, artifact_root, args):
    if target != "x86_64":
        write_unsupported(artifact_root, "openposix", target, "Open POSIX adapter is x86_64 only")
        return
    source = ROOT / "third_party" / "open_posix_testsuite" / "testcases" / "open_posix_testsuite"
    if not (source / "Makefile").is_file():
        raise RuntimeError("prepare third_party/open_posix_testsuite with tools/prepare-openposix.sh first")
    build_dir = output_dir / "openposix-build" / target / "open_posix_testsuite"
    shutil.rmtree(build_dir.parent, ignore_errors=True)
    build_dir.parent.mkdir(parents=True)
    shutil.copytree(source, build_dir, symlinks=True)
    env = {**os.environ, "CC": args.x86_64_cc}
    if shutil.which("autoconf") is None:
        raise RuntimeError("Open POSIX requires Autoconf (Debian/Ubuntu: apt install autoconf)")
    run(["make", "autotools"], cwd=build_dir, env=env,
        output=output_dir / "openposix-autotools.log")
    run(["./configure", f"CC={args.x86_64_cc}"], cwd=build_dir, env=env,
        output=output_dir / "openposix-configure.log")
    run(["make", "generate-makefiles"], cwd=build_dir, env=env,
        output=output_dir / "openposix-generate.log")
    run(["make", f"-j{args.jobs}"], cwd=build_dir, env=env,
        output=output_dir / "openposix-build.log")
    cases = sorted(build_dir.rglob("*.run-test"))
    if not cases:
        raise RuntimeError("Open POSIX build produced no .run-test cases")

    suite_root = artifact_root / "openposix" / target
    shutil.rmtree(suite_root, ignore_errors=True)
    suite_root.mkdir(parents=True)
    machine = subprocess.check_output([args.x86_64_cc, "-dumpmachine"], text=True).strip()
    if isa_for(machine) != target:
        raise RuntimeError(f"{args.x86_64_cc} targets {machine}, expected {target}")
    (suite_root / ".machine").write_text(machine + "\n", encoding="ascii")
    (suite_root / ".abi").write_text(abi_for(machine) + "\n", encoding="ascii")
    (suite_root / ".libc").write_text("glibc\n" if "musl" not in machine else "musl\n",
                                       encoding="ascii")
    archive_path = suite_root / "openposix.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(build_dir, arcname="open_posix_testsuite")
    (suite_root / ".runlist.json").write_text(json.dumps([{
        "source": f"openposix/{target}/{archive_path.name}",
        "name": f"Open POSIX ({len(cases)} cases)",
        "timeout": 60,
    }], indent=2) + "\n", encoding="utf-8")


def add_runtime_file(source, rootfs, absolute):
    destination = rootfs / absolute.lstrip("/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def stage_runtime(artifact_root, rootfs, target, suites, args):
    for directory in ("bin", "lib", "lib64", "etc", "dev", "proc", "tmp", "work", "logs"):
        path = rootfs / directory
        path.mkdir(parents=True, exist_ok=True)
        if directory in ("tmp", "work", "logs"):
            path.chmod(0o777)
    for device in ("null", "zero", "random", "urandom", "tty"):
        (rootfs / "dev" / device).touch(exist_ok=True)
    busybox_root = artifact_root / "busybox" / target
    busybox = busybox_root / "busybox"
    if not busybox.is_file():
        raise RuntimeError(f"no BusyBox runtime for {target}; cannot build a self-contained rootfs")
    loader = subprocess.check_output(["readelf", "-l", str(busybox)], text=True)
    if "Requesting program interpreter" in loader:
        raise RuntimeError("rootfs BusyBox is not static")
    available = set(subprocess.check_output([str(busybox), "--list"], text=True).splitlines()) if target == "x86_64" else None
    applets = ("sh", "cp", "chmod", "mkdir", "rm", "sed", "timeout", "ln", "cat", "echo", "uname",
               "ls", "pwd", "printf", "grep", "awk", "head", "tail", "find", "cmp", "cut", "tr", "wc")
    if available is not None:
        missing = sorted(set(applets) - available)
        if missing:
            raise RuntimeError("static BusyBox is missing required applets: " + ", ".join(missing))
    shutil.copy2(busybox, rootfs / "bin" / "busybox")
    for applet in applets:
        shutil.copy2(busybox, rootfs / "bin" / applet)
    (rootfs / "etc" / "passwd").write_text("root:x:0:0:root:/root:/bin/sh\n", encoding="ascii")
    (rootfs / "etc" / "group").write_text("root:x:0:\n", encoding="ascii")

    executables = []
    for suite in suites:
        suite_root = artifact_root / suite / target
        for path in suite_root.iterdir():
            if path.is_file() and not path.name.startswith(".") and elf_executable(path):
                executables.append(path)
        if suite == "openposix":
            built_tree = artifact_root.parent / "openposix-build" / target / "open_posix_testsuite"
            executables.extend(built_tree.rglob("*.run-test"))

    for binary in executables:
        header = subprocess.check_output(["readelf", "-l", str(binary)], text=True)
        interpreter = re.search(r"Requesting program interpreter: ([^]]+)", header)
        if not interpreter:
            continue
        if target != "x86_64":
            raise RuntimeError(f"dynamic cross-ABI runtime collection is not configured: {binary.name}")
        loader_path = interpreter.group(1)
        loader_source = Path(loader_path)
        if not loader_source.is_file():
            raise RuntimeError(f"ELF interpreter not found: {loader_path} ({binary})")
        add_runtime_file(loader_source, rootfs, loader_path)
        dependencies = subprocess.check_output(["ldd", str(binary)], text=True, stderr=subprocess.STDOUT)
        if "not found" in dependencies:
            raise RuntimeError(f"unresolved ELF dependency in {binary}:\n{dependencies}")
        for resolved in re.findall(r"=>\s+(/[^\s]+)", dependencies):
            source = Path(resolved)
            if not source.is_file():
                raise RuntimeError(f"ELF dependency not found: {resolved} ({binary})")
            add_runtime_file(source, rootfs, resolved)
    (rootfs / "ABI.TXT").write_text(
        f"arch={target}\ntarget={args.target}\nabi={args.abi}\nfilesystem={args.fs}\n",
        encoding="ascii")


def build(args):
    output_dir = args.out.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = args.targets
    image = output_dir / f"test-{target}.img"
    image.unlink(missing_ok=True)
    suites = args.suite.split(",") if args.suite != "all" else [
        "libctest", "picolibc", "openposix", "busybox"]
    if not suites or len(set(suites)) != len(suites):
        raise RuntimeError("SUITE must be a non-empty, duplicate-free list")
    if "ltp" in suites:
        raise RuntimeError("LTP is not a supported suite; select openposix instead")
    unknown = sorted(set(suites) - {"libctest", "picolibc", "openposix", "busybox"})
    if unknown:
        raise RuntimeError("unknown suites: " + ", ".join(unknown))
    if "openposix" in suites:
        suites = [suite for suite in suites if suite != "openposix"] + ["openposix"]
    if target not in ("x86_64", "rv64", "rv32", "aarch64", "loongarch64"):
        raise RuntimeError(f"unsupported ISA target: {target}")
    if args.fs not in ("fat", "ext4"):
        raise RuntimeError("FS must be fat or ext4")
    artifact_root = output_dir / "artifacts"
    shutil.rmtree(artifact_root, ignore_errors=True)
    for suite in suites:
        try:
            if suite == "libctest":
                build_libctest(target, artifact_root, args, output_dir)
            elif suite == "picolibc":
                build_picolibc(target, output_dir, artifact_root, args)
            elif suite == "openposix":
                build_openposix(target, output_dir, artifact_root, args)
            else:
                build_busybox(target, output_dir, artifact_root, args)
        except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
            print(f"BUILD_FAIL {suite}/{target}: {error}", file=sys.stderr, flush=True)
            write_build_failure(artifact_root, suite, target, error)

    # BusyBox is the image's shell/tool runtime even when its tests are not selected.
    busybox_dir = artifact_root / "busybox" / target
    if not (busybox_dir / "busybox").is_file():
        if "busybox" in suites:
            raise RuntimeError("BusyBox suite build failed; its static shell is required by the test rootfs")
        try:
            build_busybox(target, output_dir, artifact_root, args)
        except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
            if "busybox" not in suites:
                write_build_failure(artifact_root, "busybox", target, error)
            raise RuntimeError(f"cannot produce a runnable rootfs without BusyBox: {error}") from error

    valid_tests = 0
    build_failures = []
    for suite in suites:
        sidecar = artifact_root / suite / target / ".runlist.json"
        if not sidecar.is_file():
            continue
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else [data]
        valid_tests += sum(row.get("status", "BUILT") == "BUILT" for row in rows)
        build_failures.extend(f"{suite}: {row.get('reason', 'build failed')}"
                              for row in rows if row.get("status") == "BUILD_FAIL")
    if valid_tests == 0:
        raise RuntimeError("no selected Suite produced an executable test; refusing to report a successful image")

    runtime_root = output_dir / "rootfs"
    shutil.rmtree(runtime_root, ignore_errors=True)
    stage_runtime(artifact_root, runtime_root, target, suites, args)
    run([sys.executable, str(IMAGE), "pack", "--input", str(artifact_root),
         "--suites", ",".join(suites), "--fs", args.fs,
         "--arch", target, "--runtime", str(runtime_root), "--output", str(image)])
    run([sys.executable, str(IMAGE), "verify", "--input", str(artifact_root),
         "--suites", ",".join(suites), "--fs", args.fs,
         "--arch", target, "--runtime", str(runtime_root), "--image", str(image)])
    print(f"test image ready: {image}")
    if build_failures:
        raise RuntimeError("one or more selected Suite builds failed: " + "; ".join(build_failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="all", help="all or comma-separated suite names")
    parser.add_argument("--targets", default="x86_64", help="one ISA for this image")
    parser.add_argument("--fs", default="fat", help="disk filesystem: fat or ext4")
    parser.add_argument("--target", choices=("linux", "kaleid"), default="linux",
                        help="intended execution environment; never starts tests")
    parser.add_argument("--abi", choices=("linux-gnu", "linux-musl", "kaleidos-posix"),
                        help="runtime ABI recorded in ABI.TXT")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--x86-64-cc", dest="x86_64_cc", default="gcc")
    parser.add_argument("--rv64-cc", default="riscv64-linux-gnu-gcc")
    parser.add_argument("--rv32-cc", default="riscv32-linux-gnu-gcc")
    parser.add_argument("--aarch64-cc", default="aarch64-linux-gnu-gcc")
    parser.add_argument("--loongarch64-cc", default="loongarch64-linux-gnu-gcc")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    try:
        if "," in args.targets:
            raise RuntimeError("build one ISA per image; pass one value to --targets")
        if args.abi is None:
            args.abi = "kaleidos-posix" if args.target == "kaleid" else "linux-gnu"
        if args.jobs < 1:
            raise RuntimeError("--jobs must be positive")
        build(args)
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as error:
        print(f"test build: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
