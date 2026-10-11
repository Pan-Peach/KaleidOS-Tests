#!/usr/bin/env python3
"""Create and verify the shared Suite/ISA FAT test disk."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def command(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT)
    except FileNotFoundError as error:
        raise RuntimeError(f"missing host tool: {args[0]}") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.output.strip()) from error


def entries(root, suites):
    result = []
    for suite in suites:
        suite_root = root / suite
        if not suite_root.is_dir():
            raise RuntimeError(f"no built artifacts for suite {suite}; build it first")
        for isa_root in sorted(path for path in suite_root.iterdir() if path.is_dir()):
            files = sorted(path for path in isa_root.iterdir()
                           if path.is_file() and not path.name.startswith("."))
            if not files:
                raise RuntimeError(f"empty artifact directory: {isa_root}")
            for path in files:
                relative = path.relative_to(root).as_posix()
                header = path.read_bytes()[:4]
                kind = "ELF" if header == b"\x7fELF" else "PE" if header[:2] == b"MZ" else "DATA"
                result.append((path, relative, kind))
    if not result:
        raise RuntimeError("selected suites produced no files")
    return result


def pack(args):
    selected = entries(args.input.resolve(), args.suites.split(","))
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="kaleidos-test-image-") as temporary:
        image = Path(temporary) / "tests.img"
        image.write_bytes(bytes(64 * 1024 * 1024))
        command(["mkfs.vfat", "-F", "32", "-n", "KALEIDOST", str(image)])
        directories = sorted({str(Path(relative).parent) for _source, relative, _kind in selected},
                            key=lambda item: (item.count("/"), item))
        created = set()
        for directory in directories:
            parts = Path(directory).parts
            for index in range(1, len(parts) + 1):
                parent = "/".join(parts[:index])
                if parent not in created:
                    command(["mmd", "-i", str(image), f"::/{parent}"])
                    created.add(parent)
        for source, relative, _kind in selected:
            command(["mcopy", "-o", "-i", str(image), str(source), f"::/{relative}"])
        lines = ["KaleidOS test image; binaries are manually run by ISA and execution support.",
                 "suite/isa/path | format | environment"]
        for source, relative, kind in selected:
            suite, isa, _name = relative.split("/", 2)
            machine_file = source.parent / ".machine"
            machine = machine_file.read_text(encoding="ascii").strip() if machine_file.is_file() else "unknown"
            if kind == "PE":
                environment = "Windows x86_64; no KaleidOS PE loader"
            elif isa == "x86_64":
                environment = "Linux x86_64 native reference"
            else:
                environment = f"{machine}; KaleidOS syscall compatibility unverified"
            lines.append(f"{relative} | {kind} | {environment}")
        tests_file = Path(temporary) / "TESTS.TXT"
        tests_file.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
        command(["mcopy", "-o", "-i", str(image), str(tests_file), "::/TESTS.TXT"])
        shutil.copyfile(image, output)
    print(f"image: {output} ({output.stat().st_size} bytes, {len(selected)} artifacts)")
    return 0


def verify(args):
    image = args.image.resolve()
    if not image.is_file():
        raise RuntimeError(f"image not found: {image}")
    found = entries(args.input.resolve(), args.suites.split(","))
    listing = command(["mdir", "-i", str(image), "::/"])
    if not re.search(r"TESTS\s+TXT", listing):
        raise RuntimeError("TESTS.TXT is missing from FAT root")
    with tempfile.TemporaryDirectory(prefix="kaleidos-test-verify-") as temporary:
        destination = Path(temporary)
        reference_rows = []
        log_root = args.logs.resolve() if args.logs else None
        if args.run_native and log_root:
            log_root.mkdir(parents=True, exist_ok=True)
        for _source, relative, _kind in found:
            suite, isa, _name = relative.split("/", 2)
            command(["mdir", "-i", str(image), f"::/{suite}/{isa}"])
            extracted = destination / relative
            extracted.parent.mkdir(parents=True, exist_ok=True)
            command(["mcopy", "-i", str(image), f"::/{relative}", str(extracted)])
            original = args.input.resolve() / relative
            if extracted.stat().st_size != original.stat().st_size:
                raise RuntimeError(f"size changed in FAT image: {relative}")
            command(["cmp", "--", str(original), str(extracted)])
            if args.run_native and isa == "x86_64" and _kind == "ELF":
                extracted.chmod(0o755)
                run_directory = destination / ("run-" + relative.replace("/", "_"))
                run_directory.mkdir()
                if relative.endswith("/posix_stat"):
                    time.sleep(1.1 - (time.time() % 1))
                try:
                    process = subprocess.run([str(extracted)], cwd=run_directory,
                                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, timeout=30)
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise RuntimeError(f"extracted Linux reference failed: {relative}: {error}") from error
                status = "PASS" if process.returncode == 0 else "FAIL"
                reference_rows.append((relative, status, process.returncode))
                if log_root:
                    stem = relative.replace("/", "_")
                    (log_root / f"{stem}.stdout.log").write_bytes(process.stdout)
                    (log_root / f"{stem}.stderr.log").write_bytes(process.stderr)
                print(f"extracted Linux reference {relative}: {status} (exit {process.returncode})")
                if process.returncode != 0:
                    raise RuntimeError(f"extracted Linux reference exited {process.returncode}: {relative}")
        test_list = destination / "TESTS.TXT"
        command(["mcopy", "-i", str(image), "::/TESTS.TXT", str(test_list)])
        declared = test_list.read_text(encoding="ascii")
        for _source, relative, _kind in found:
            if relative not in declared:
                raise RuntimeError(f"missing manifest row: {relative}")
        if reference_rows and log_root:
            report = ["path\tstatus\texit_status"]
            report.extend(f"{path}\t{status}\t{code}" for path, status, code in reference_rows)
            (log_root / "extracted-reference.tsv").write_text("\n".join(report) + "\n",
                                                               encoding="utf-8")
    print(f"FAT verified: {image} ({len(found)} artifacts readable and identical)")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("pack", "verify"):
        sub = subparsers.add_parser(action)
        sub.add_argument("--input", type=Path, required=True, help="suite/isa artifact root")
        sub.add_argument("--suites", required=True, help="comma-separated suite names")
        if action == "pack":
            sub.add_argument("--output", type=Path, required=True)
            sub.set_defaults(run=pack)
        else:
            sub.add_argument("--image", type=Path, required=True)
            sub.add_argument("--run-native", action="store_true",
                             help="run extracted x86_64 Linux ELF files on this Linux host")
            sub.add_argument("--logs", type=Path, help="directory for extracted stdout/stderr and status")
            sub.set_defaults(run=verify)
    args = parser.parse_args()
    try:
        return args.run(args)
    except (OSError, RuntimeError, UnicodeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
