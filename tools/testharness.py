#!/usr/bin/env python3
"""Run the tests supported by this Linux host from a mounted FAT image."""

import argparse
from itertools import groupby
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile


def host_isa():
    machine = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "riscv64": "rv64",
            "riscv32": "rv32", "aarch64": "aarch64"}.get(machine)


def run_one(path, args=(), env=None, timeout=30, cwd=None):
    environment = {**os.environ, **(env or {})}
    if env and env.get("PATH"):
        environment["PATH"] = env["PATH"] + os.pathsep + os.environ.get("PATH", "")
    process = subprocess.run([str(path), *map(str, args)], cwd=cwd,
                             env=environment, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=max(1, int(timeout)))
    return process.returncode, process.stdout.decode("utf-8", errors="replace")


def color_enabled(mode):
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", action="append", help="run only this suite; repeatable")
    parser.add_argument("--logs", type=Path, help="write the report and per-test output here")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "RUNLIST.TXT").read_text(encoding="utf-8"))
    selected = set(args.suite or ())
    host = platform.system().lower()
    isa = host_isa()
    rows = []
    use_color = color_enabled(args.color)
    colors = {"PASS": "\033[32m", "FAIL": "\033[31m", "WARN": "\033[33m",
              "SKIP": "\033[33m", "HEADING": "\033[1;36m", "RESET": "\033[0m"}

    def paint(text, kind):
        return f"{colors[kind]}{text}{colors['RESET']}" if use_color else text

    output_dir = args.logs.resolve() if args.logs else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    def record(name, status, detail="", output=""):
        row = {"name": name, "status": status, "detail": detail}
        rows.append(row)
        label = paint(status.ljust(7), status)
        print(f"{label} {name}" + (f" ({detail})" if detail else ""), flush=True)
        if output_dir and output:
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
            (output_dir / f"{safe}.log").write_text(output, encoding="utf-8")

    if host != "linux":
        print(f"No tests selected: this harness currently runs Linux host tests (detected {host}).")
        return 2
    if not isa:
        print(f"No tests selected: unsupported host ISA {platform.machine()}.")
        return 2

    suites = [(suite, list(items)) for suite, items in groupby(manifest, key=lambda item: item["suite"])]
    for suite, items in suites:
        if selected and suite not in selected:
            continue
        print(paint(f"\n{'=' * 18} SUITE: {suite} {'=' * 18}", "HEADING"), flush=True)
        before = len(rows)
        for item in items:
            if item.get("isa") != isa:
                if suite == "busybox" or item.get("image") or item.get("tests"):
                    record(item.get("name", suite), "SKIP",
                           f"requires {item.get('isa')}; host is {isa}")
                continue
            if suite == "busybox":
                name = "busybox/testsuite (" + ", ".join(item["applets"]) + ")"
                try:
                    with tempfile.TemporaryDirectory(prefix="kaleidos-busybox-") as temp:
                        work = Path(temp)
                        with tarfile.open(root / item["tests"], "r:gz") as archive:
                            if sys.version_info >= (3, 12):
                                archive.extractall(work, filter="data")
                            else:
                                archive.extractall(work)
                        test_dir = work / "testsuite"
                        bindir = work / "bin"
                        bindir.mkdir()
                        for key, target in (("binary", "busybox"), ("config", "bbconfig.data"),
                                            ("echo", "echo-ne")):
                            shutil.copy2(root / item[key], bindir / target)
                        (bindir / "busybox").chmod(0o755)
                        (bindir / "echo-ne").chmod(0o755)
                        (test_dir / "busybox").symlink_to(bindir / "busybox")
                        env = {"PATH": f"{bindir}:{os.environ.get('PATH', '')}",
                               "tsdir": str(test_dir), "bindir": str(bindir),
                               "SKIP_INTERNET_TESTS": "1", "ECHO": str(bindir / "echo-ne")}
                        code, output = run_one(shutil.which("sh") or "/bin/sh",
                                               [str(test_dir / "runtest"), *item["applets"]],
                                               env=env, timeout=180, cwd=test_dir)
                        record(name, "PASS" if code == 0 else "FAIL", f"exit {code}", output)
                except (OSError, tarfile.TarError, subprocess.TimeoutExpired) as error:
                    record(name, "FAIL", str(error))
                continue

            if suite == "openposix":
                try:
                    with tempfile.TemporaryDirectory(prefix="kaleidos-openposix-") as temp:
                        work = Path(temp)
                        with tarfile.open(root / item["image"], "r:gz") as archive:
                            if sys.version_info >= (3, 12):
                                archive.extractall(work, filter="data")
                            else:
                                archive.extractall(work)
                        test_root = work / "open_posix_testsuite"
                        cases = sorted(test_root.rglob("*.run-test"))
                        for case in cases:
                            name = "openposix/" + case.relative_to(test_root).with_suffix("").as_posix()
                            try:
                                code, output = run_one(shutil.which("sh") or "/bin/sh",
                                                       [str(case)], timeout=item.get("timeout", 60),
                                                       cwd=case.parent)
                                status = "PASS" if code == 0 else "WARN" if code == 2 else "FAIL"
                                record(name, status, f"exit {code}", output)
                            except (OSError, subprocess.TimeoutExpired) as error:
                                record(name, "FAIL", str(error))
                        if not cases:
                            record("openposix", "FAIL", "archive contains no .run-test cases")
                except (OSError, tarfile.TarError) as error:
                    record("openposix", "FAIL", str(error))
                continue

            name = item.get("name", suite)
            if suite == "libctest" and item.get("status") == "UNSUPPORTED":
                record(name, "SKIP", item.get("reason") or "upstream case is unsupported")
                continue
            if suite == "libctest" and item.get("status") != "BUILT":
                record(name, "FAIL", item.get("reason") or "test build failed")
                continue
            if not item.get("image"):
                record(name, "SKIP", "no executable in the image")
                continue
            binary = root / item["image"]
            try:
                with tempfile.TemporaryDirectory(prefix="kaleidos-test-") as temp:
                    local_binary = Path(temp) / "test-program"
                    shutil.copy2(binary, local_binary)
                    local_binary.chmod(0o755)
                    code, output = run_one(local_binary, item.get("args", []), item.get("env", {}),
                                           item.get("timeout", 30), cwd=temp)
                record(name, "PASS" if code == 0 else "FAIL", f"exit {code}", output)
            except (OSError, subprocess.TimeoutExpired) as error:
                record(name, "FAIL", str(error))
        suite_rows = rows[before:]
        counts = {status: sum(row["status"] == status for row in suite_rows)
                  for status in ("PASS", "FAIL", "WARN", "SKIP")}
        print(paint(f"{'-' * 56}\nSuite {suite}: {counts['PASS']} passed, "
                    f"{counts['FAIL']} failed, {counts['WARN'] + counts['SKIP']} warnings/skips\n"
                    f"{'-' * 56}", "HEADING"), flush=True)

    if not rows:
        print("No matching tests were packaged for this host.")
        return 2
    report = {"host": platform.platform(), "isa": isa, "results": rows}
    text = json.dumps(report, indent=2) + "\n"
    try:
        (root / "RESULTS.TXT").write_text(text, encoding="utf-8")
    except OSError as error:
        print(f"Could not write RESULTS.TXT on the mounted image: {error}")
    if output_dir:
        (output_dir / "RESULTS.TXT").write_text(text, encoding="utf-8")
    passed = sum(row["status"] == "PASS" for row in rows)
    failed = sum(row["status"] == "FAIL" for row in rows)
    skipped = sum(row["status"] == "SKIP" for row in rows)
    warnings = sum(row["status"] == "WARN" for row in rows)
    print(f"Summary: {paint(str(passed), 'PASS')} passed, {paint(str(failed), 'FAIL')} failed, "
          f"{paint(str(warnings + skipped), 'WARN')} warnings/skips")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(main())
