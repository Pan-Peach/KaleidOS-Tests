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
            used = set()
            for path in files:
                source_relative = path.relative_to(root).as_posix()
                header = path.read_bytes()[:4]
                kind = "ELF" if header == b"\x7fELF" else "PE" if header[:2] == b"MZ" else "DATA"
                extension = {"ELF": "ELF", "PE": "EXE", "DATA": "DAT"}[kind]
                stem = path.stem.upper()
                for prefix in ("COMPILER_", "LIBC_", "POSIX_"):
                    if stem.startswith(prefix):
                        stem = stem[len(prefix):]
                        break
                stem = re.sub(r"[^A-Z0-9]", "", stem) or "TEST"
                base = stem[:8]
                candidate = f"{base}.{extension}"
                suffix = 1
                while candidate in used:
                    tail = str(suffix)
                    candidate = f"{base[:8-len(tail)]}{tail}.{extension}"
                    suffix += 1
                used.add(candidate)
                image_relative = f"{isa_root.parent.name}/{isa_root.name}/{candidate}"
                result.append((path, source_relative, image_relative, kind))
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
        directories = sorted({str(Path(image_relative).parent)
                              for _source, _source_relative, image_relative, _kind in selected},
                            key=lambda item: (item.count("/"), item))
        created = set()
        for directory in directories:
            parts = Path(directory).parts
            for index in range(1, len(parts) + 1):
                parent = "/".join(parts[:index])
                if parent not in created:
                    command(["mmd", "-i", str(image), f"::/{parent}"])
                    created.add(parent)
        for source, _source_relative, image_relative, _kind in selected:
            command(["mcopy", "-o", "-i", str(image), str(source), f"::/{image_relative}"])
        lines = ["KaleidOS test image; binaries are manually run by ISA and execution support.",
                 "image path (8.3) | format and ABI | original test name"]
        for source, source_relative, image_relative, kind in selected:
            _suite, isa, _name = image_relative.split("/", 2)
            machine_file = source.parent / ".machine"
            machine = machine_file.read_text(encoding="ascii").strip() if machine_file.is_file() else "unknown"
            if kind == "PE":
                environment = f"{machine}; Windows PE, no KaleidOS PE loader"
            elif isa == "x86_64":
                environment = f"{machine}; Linux x86_64 native reference"
            else:
                environment = f"{machine}; KaleidOS syscall compatibility unverified"
            lines.append(f"{image_relative} | {kind} {environment} | source {source_relative}")
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
        groups = {}
        for _source, _source_relative, image_relative, _kind in found:
            suite, isa, _name = image_relative.split("/", 2)
            groups.setdefault((suite, isa), []).append(image_relative)
        for (suite, isa), paths in groups.items():
            listing = command(["mdir", "-i", str(image), f"::/{suite}/{isa}"])
            aliases = re.findall(r"^([A-Z0-9_~]{1,8})\s+([A-Z0-9_~]{1,3})\s+\d+\s",
                                 listing, re.MULTILINE)
            short_names = [f"{base}.{extension}" for base, extension in aliases]
            if len(short_names) != len(paths) or len(set(short_names)) != len(short_names):
                raise RuntimeError(f"FAT 8.3 names are missing or collide in {suite}/{isa}")
        for _source, source_relative, image_relative, kind in found:
            suite, isa, _name = image_relative.split("/", 2)
            extracted = destination / image_relative
            extracted.parent.mkdir(parents=True, exist_ok=True)
            command(["mcopy", "-i", str(image), f"::/{image_relative}", str(extracted)])
            original = args.input.resolve() / source_relative
            if extracted.stat().st_size != original.stat().st_size:
                raise RuntimeError(f"size changed in FAT image: {image_relative}")
            command(["cmp", "--", str(original), str(extracted)])
            if args.run_native and isa == "x86_64" and kind == "ELF":
                extracted.chmod(0o755)
                run_directory = destination / ("run-" + source_relative.replace("/", "_"))
                run_directory.mkdir()
                if source_relative.endswith("/posix_stat"):
                    time.sleep(1.1 - (time.time() % 1))
                try:
                    process = subprocess.run([str(extracted)], cwd=run_directory,
                                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, timeout=30)
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise RuntimeError(f"extracted Linux reference failed: {source_relative}: {error}") from error
                status = "PASS" if process.returncode == 0 else "FAIL"
                reference_rows.append((source_relative, status, process.returncode))
                if log_root:
                    stem = source_relative.replace("/", "_")
                    (log_root / f"{stem}.stdout.log").write_bytes(process.stdout)
                    (log_root / f"{stem}.stderr.log").write_bytes(process.stderr)
                print(f"extracted Linux reference {source_relative}: {status} (exit {process.returncode})")
                if process.returncode != 0:
                    raise RuntimeError(f"extracted Linux reference exited {process.returncode}: {source_relative}")
        test_list = destination / "TESTS.TXT"
        command(["mcopy", "-i", str(image), "::/TESTS.TXT", str(test_list)])
        declared = test_list.read_text(encoding="ascii")
        for _source, source_relative, image_relative, _kind in found:
            if image_relative not in declared or f"source {source_relative}" not in declared:
                raise RuntimeError(f"missing manifest mapping: {source_relative}")
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
