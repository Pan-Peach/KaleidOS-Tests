#!/usr/bin/env python3
"""Create and verify the shared Suite/ISA FAT or ext4 test disk."""

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

from shell_image import prepare


def command(args):
    try:
        return subprocess.check_output(args, stdin=subprocess.DEVNULL, text=True, stderr=subprocess.STDOUT)
    except FileNotFoundError as error:
        raise RuntimeError(f"missing host tool: {args[0]}") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(error.output.strip()) from error


def set_difference(expected, actual):
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return f"missing={missing[:8]} (+{max(0, len(missing) - 8)}), extra={extra[:8]} (+{max(0, len(extra) - 8)})"


def fat_component(name):
    if "." in name:
        stem, ext = name.rsplit(".", 1)
        stem = re.sub(r"[^A-Z0-9_~-]", "", stem.upper())[:8] or "DIR"
        ext = re.sub(r"[^A-Z0-9_~-]", "", ext.upper())[:3]
        return f"{stem}.{ext}" if ext else stem
    stem = re.sub(r"[^A-Z0-9_~-]", "", name.upper())[:8] or "DIR"
    return stem


def entries(root, suites, arch, filesystem="fat"):
    result = []
    used_names = {}
    for suite in suites:
        suite_root = root / suite / arch
        if not suite_root.is_dir():
            raise RuntimeError(f"no {arch} artifacts for suite {suite}; build it first")
        all_files = sorted(
            p for p in suite_root.rglob("*")
            if p.is_file() and not (p.name.startswith(".") and p.name not in (".keep", ".gitignore"))
        )
        if filesystem == "ext4":
            for path in all_files:
                source_relative = path.relative_to(root).as_posix()
                header = path.read_bytes()[:20]
                if header[:4] == b"\x7fELF":
                    kind = "ELF"
                elif header[:4] == b"\x00asm":
                    kind = "WASM"
                else:
                    kind = "DATA"
                rel_inside = path.relative_to(suite_root).as_posix()
                image_relative = f"{suite}/{rel_inside}"
                result.append((path, source_relative, image_relative, kind))
        else:
            by_dir = {}
            for path in all_files:
                rel_dir = path.relative_to(suite_root).parent
                by_dir.setdefault(rel_dir, []).append(path)

            for rel_dir, files in by_dir.items():
                dir_key = f"{suite}/{rel_dir.as_posix()}" if rel_dir.parts else suite
                used_in_dir = used_names.setdefault(dir_key, set())
                used_stems = set()
                stem_map = {}

                wasms = [f for f in files if f.suffix.lower() == ".wasm" or f.read_bytes()[:4] == b"\x00asm"]
                others = [f for f in files if f not in wasms]

                for path in wasms:
                    source_relative = path.relative_to(root).as_posix()
                    kind = "WASM"
                    clean_stem = re.sub(r"[^A-Z0-9_~-]", "", path.stem.upper()) or "TEST"
                    base = clean_stem[:8]
                    cand_stem = base
                    suf = 1
                    while cand_stem in used_stems or f"{cand_stem}.WAS" in used_in_dir:
                        tail = str(suf)
                        cand_stem = f"{base[:8-len(tail)]}{tail}"
                        suf += 1
                    used_stems.add(cand_stem)
                    stem_map[path.stem] = cand_stem
                    candidate = f"{cand_stem}.WAS"
                    used_in_dir.add(candidate)

                    fat_dir = "/".join(fat_component(p) for p in rel_dir.parts)
                    image_relative = f"{suite}/{fat_dir}/{candidate}" if fat_dir else f"{suite}/{candidate}"
                    result.append((path, source_relative, image_relative, kind))

                for path in others:
                    source_relative = path.relative_to(root).as_posix()
                    header = path.read_bytes()[:20]
                    if header[:4] == b"\x7fELF":
                        kind = "ELF"
                    elif header[:4] == b"\x00asm":
                        kind = "WASM"
                    else:
                        kind = "DATA"

                    if path.suffix.lower() == ".json":
                        ext = "JSN"
                        if path.stem in stem_map:
                            candidate = f"{stem_map[path.stem]}.{ext}"
                        else:
                            clean_stem = re.sub(r"[^A-Z0-9_~-]", "", path.stem.upper()) or "DATA"
                            base = clean_stem[:8]
                            cand_stem = base
                            suf = 1
                            while cand_stem in used_stems or f"{cand_stem}.{ext}" in used_in_dir:
                                tail = str(suf)
                                cand_stem = f"{base[:8-len(tail)]}{tail}"
                                suf += 1
                            used_stems.add(cand_stem)
                            candidate = f"{cand_stem}.{ext}"
                    elif path.name.startswith("."):
                        clean_stem = re.sub(r"[^A-Z0-9_~-]", "", path.name[1:].upper()) or "DATA"
                        base = clean_stem[:8]
                        candidate = f"{base}.DAT"
                    elif path.suffix:
                        ext = re.sub(r"[^A-Z0-9_~-]", "", path.suffix[1:].upper())[:3] or "DAT"
                        clean_stem = re.sub(r"[^A-Z0-9_~-]", "", path.stem.upper()) or "FILE"
                        base = clean_stem[:8]
                        candidate = f"{base}.{ext}"
                    else:
                        clean_stem = re.sub(r"[^A-Z0-9_~-]", "", path.stem.upper()) or "FILE"
                        candidate = clean_stem[:8]

                    if candidate in used_in_dir:
                        base_part, *ext_parts = candidate.split(".", 1)
                        ext_part = f".{ext_parts[0]}" if ext_parts else ""
                        suf = 1
                        while candidate in used_in_dir:
                            tail = str(suf)
                            candidate = f"{base_part[:8-len(tail)]}{tail}{ext_part}"
                            suf += 1
                    used_in_dir.add(candidate)

                    fat_dir = "/".join(fat_component(p) for p in rel_dir.parts)
                    image_relative = f"{suite}/{fat_dir}/{candidate}" if fat_dir else f"{suite}/{candidate}"
                    result.append((path, source_relative, image_relative, kind))

    if not result:
        raise RuntimeError("selected suites produced no files")
    return result


def runtime_entries(root, filesystem):
    result = []
    used = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if filesystem == "ext4":
            image_relative = relative
        else:
            parent = str(Path(relative).parent)
            if relative == "ABI.TXT":
                image_relative = "ABI.TXT"
                result.append((path, relative, image_relative, "DATA"))
                continue
            is_applet = parent == "bin"
            extension = "" if is_applet else "ELF" if path.read_bytes()[:4] == b"\x7fELF" else "DAT"
            stem = re.sub(r"[^A-Z0-9]", "", (path.stem if not is_applet else path.name).upper()) or "FILE"
            names = used.setdefault(parent, set())
            base = stem[:8]
            candidate = f"{base}.{extension}" if extension else base
            suffix = 1
            while candidate in names:
                tail = str(suffix)
                short = f"{base[:8-len(tail)]}{tail}"
                candidate = f"{short}.{extension}" if extension else short
                suffix += 1
            names.add(candidate)
            image_relative = f"{parent}/{candidate}" if parent != "." else candidate
        result.append((path, relative, image_relative,
                       "ELF" if path.read_bytes()[:4] == b"\x7fELF" else "DATA"))
    return result


def pack(args):
    if args.fs not in ("fat", "ext4"):
        raise RuntimeError("FS must be fat or ext4")
    selected = entries(args.input.resolve(), args.suites.split(","), args.arch, args.fs)
    runtime_root = args.runtime.resolve()
    if not runtime_root.is_dir():
        raise RuntimeError(f"runtime root not found: {runtime_root}")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="kaleidos-test-image-") as temporary:
        selected, cases_file, wasi_manifest_file, wasi_json_file = prepare(
            args.input.resolve(), args.suites.split(","),
            args.arch, selected, Path(temporary) / "shell")
        selected += runtime_entries(runtime_root, args.fs)
        image = Path(temporary) / "tests.img"
        payload_bytes = sum(source.stat().st_size for source, *_rest in selected)
        if wasi_manifest_file:
            payload_bytes += wasi_manifest_file.stat().st_size + wasi_json_file.stat().st_size
        reserve = max(4 * 1024 * 1024, payload_bytes // 8)
        image_bytes = max(64 * 1024 * 1024, payload_bytes + reserve + 32 * 1024 * 1024)
        image_bytes = ((image_bytes + 16 * 1024 * 1024 - 1) // (16 * 1024 * 1024)) * 16 * 1024 * 1024
        image.touch()
        with image.open("r+b") as disk:
            disk.truncate(image_bytes)
        runlist_file = cases_file
        harness = Path(__file__).with_name("testharness.sh")
        if not harness.is_file():
            raise RuntimeError("tools/testharness.sh is missing")
        metadata = (runtime_root / "ABI.TXT").read_text(encoding="ascii")
        lines = [f"KaleidOS {args.arch} {args.abi} {args.fs} test disk; run sh /RUNTEST.SH.",
                 metadata.strip(),
                 "image path | format and ABI | original test name"]
        for source, source_relative, image_relative, kind in selected:
            parts = image_relative.split("/", 1)
            suite = parts[0] if parts[0] in args.suites.split(",") else "runtime"
            machine_file = args.input.resolve() / suite / args.arch / ".machine"
            machine = machine_file.read_text(encoding="ascii").strip() if machine_file.is_file() else "unknown"
            environment = f"{machine}; {args.abi}; runtime/test ABI is recorded in RUNLIST.TXT"
            lines.append(f"{image_relative} | {kind} {environment} | source {source_relative}")
        tests_file = Path(temporary) / "TESTS.TXT"
        tests_file.write_text("\r\n".join(lines) + "\r\n", encoding="ascii")
        if args.fs == "fat":
            command(["mkfs.vfat", "-F", "32", "-n", "KALEIDOST", str(image)])
            directories = sorted({str(Path(image_relative).parent)
                                  for _source, _source_relative, image_relative, _kind in selected}
                                 | {path.relative_to(runtime_root).as_posix()
                                    for path in runtime_root.rglob("*") if path.is_dir()},
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
            root_copies = [
                (harness, "RUNTEST.SH"),
                (runlist_file, "RUNLIST.TXT"),
                (tests_file, "TESTS.TXT"),
            ]
            if wasi_manifest_file:
                root_copies.append((wasi_manifest_file, "WASIRUN.TXT"))
                root_copies.append((wasi_json_file, "WASIRUN.JSN"))
            for source, destination in root_copies:
                command(["mcopy", "-o", "-i", str(image), str(source), f"::/{destination}"])
        else:
            stage = Path(temporary) / "ext4-root"
            stage.mkdir()
            shutil.copytree(runtime_root, stage, dirs_exist_ok=True)
            for source, _source_relative, image_relative, _kind in selected:
                destination = stage / image_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            shutil.copy2(harness, stage / "RUNTEST.SH")
            shutil.copy2(runlist_file, stage / "RUNLIST.TXT")
            shutil.copy2(tests_file, stage / "TESTS.TXT")
            if wasi_manifest_file:
                shutil.copy2(wasi_manifest_file, stage / "WASIRUN.TXT")
                shutil.copy2(wasi_json_file, stage / "WASIRUN.JSON")
            command(["mke2fs", "-q", "-F", "-t", "ext4", "-L", "KALEIDOST",
                     "-b", "4096", "-I", "256", "-O", "64bit,metadata_csum,^orphan_file",
                     "-d", str(stage), str(image)])
        shutil.copyfile(image, output)
    print(f"image: {output} ({args.fs}, {output.stat().st_size} bytes, {len(selected)} artifacts)")
    return 0


def verify(args):
    found = entries(args.input.resolve(), args.suites.split(","), args.arch, args.fs)
    with tempfile.TemporaryDirectory(prefix="kaleidos-shell-verify-") as temporary:
        found, cases_file, wasi_manifest_file, wasi_json_file = prepare(
            args.input.resolve(), args.suites.split(","),
            args.arch, found, Path(temporary))
        found += runtime_entries(args.runtime.resolve(), args.fs)
        return verify_prepared(args, found, cases_file, wasi_manifest_file, wasi_json_file)


def verify_wasirun(manifest, expected_files, expected_directories=None):
    if manifest is None:
        return
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != 11:
            raise RuntimeError(f"invalid wasi run manifest row (expected 11 fields): {line}")
        test_id, suite, version, lang, name, wasm_path, json_path, root_dir, exit_code, status, reason = fields
        if wasm_path not in expected_files:
            raise RuntimeError(f"WASI wasm artifact missing from image: {wasm_path} in {line}")
        if json_path != "-" and json_path not in expected_files:
            raise RuntimeError(f"WASI json config missing from image: {json_path} in {line}")
        if root_dir != "-" and expected_directories is not None and root_dir not in expected_directories:
            raise RuntimeError(f"WASI fixture root missing from image: {root_dir} in {line}")


def verify_prepared(args, found, cases_file, wasi_manifest_file=None, wasi_json_file=None):
    image = args.image.resolve()
    if not image.is_file():
        raise RuntimeError(f"image not found: {image}")
    if args.fs == "ext4":
        return verify_ext4(args, image, found, cases_file, wasi_manifest_file, wasi_json_file)
    if args.fs != "fat":
        raise RuntimeError("FS must be fat or ext4")
    listing = command(["mdir", "-i", str(image), "::/"])
    for required in (r"TESTS\s+TXT", r"RUNLIST\s+TXT", r"RUNTEST\s+SH"):
        if not re.search(required, listing):
            raise RuntimeError(f"FAT root file is missing: {required}")
    if wasi_manifest_file:
        for required in (r"WASIRUN\s+TXT", r"WASIRUN\s+JSN"):
            if not re.search(required, listing):
                raise RuntimeError(f"FAT root file is missing: {required}")
    recursive = command(["mdir", "-s", "-/", "-i", str(image), "::/"])
    current_directory = ""
    actual_files = set()
    actual_directories = set()
    for line in recursive.splitlines():
        directory = re.match(r"Directory for ::/?([^\s]*)", line)
        if directory:
            current_directory = directory.group(1).strip("/")
            if current_directory:
                actual_directories.add(current_directory)
            continue
        file_entry = re.match(r"([A-Z0-9_~-]{1,8})(?:\s+([A-Z0-9_~-]{1,3}))?\s+\d+\s", line)
        if file_entry:
            name = file_entry.group(1)
            if file_entry.group(2):
                name += f".{file_entry.group(2)}"
            actual_files.add(f"{current_directory}/{name}" if current_directory else name)
    expected_files = {image_relative for _source, _source_relative, image_relative, _kind in found}
    expected_files.update({"TESTS.TXT", "RUNLIST.TXT", "RUNTEST.SH", "ABI.TXT"})
    if wasi_manifest_file:
        expected_files.add("WASIRUN.TXT")
        expected_files.add("WASIRUN.JSN")
    expected_directories = set()
    for relative in expected_files:
        parts = Path(relative).parts[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add("/".join(parts[:index]))
    expected_directories.update(path.relative_to(args.runtime.resolve()).as_posix()
                                for path in args.runtime.resolve().rglob("*") if path.is_dir())
    if actual_files != expected_files:
        raise RuntimeError("FAT file list differs: " + set_difference(expected_files, actual_files))
    if actual_directories != expected_directories:
        raise RuntimeError("FAT directories differ: " + set_difference(expected_directories, actual_directories))
    with tempfile.TemporaryDirectory(prefix="kaleidos-test-verify-") as temporary:
        destination = Path(temporary)
        reference_rows = []
        log_root = args.logs.resolve() if args.logs else None
        if args.run_native and log_root:
            log_root.mkdir(parents=True, exist_ok=True)
        directories = sorted({str(Path(path).parent) for path in expected_files if "/" in path})
        for directory in directories:
            listing = command(["mdir", "-i", str(image), f"::/{directory}"])
            aliases = re.findall(r"^([A-Z0-9_~-]{1,8})(?:\s+([A-Z0-9_~-]{1,3}))?\s+\d+\s",
                                 listing, re.MULTILINE)
            short_names = [f"{base}.{extension}" if extension else base
                           for base, extension in aliases]
            expected_in_directory = [path for path in expected_files
                                     if str(Path(path).parent) == directory]
            if len(short_names) != len(expected_in_directory) or len(set(short_names)) != len(short_names):
                raise RuntimeError(f"FAT 8.3 names are missing or collide in {directory}")
        for source, source_relative, image_relative, kind in found:
            extracted = destination / image_relative
            extracted.parent.mkdir(parents=True, exist_ok=True)
            extracted.unlink(missing_ok=True)
            command(["mcopy", "-i", str(image), f"::/{image_relative}", str(extracted)])
            original = source
            if extracted.stat().st_size != original.stat().st_size:
                raise RuntimeError(f"size changed in FAT image: {image_relative}")
            command(["cmp", "--", str(original), str(extracted)])
            suite_name = image_relative.split("/", 1)[0]
            if args.run_native and suite_name == "libctest" and args.arch == "x86_64" and kind == "ELF":
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
        test_list.unlink(missing_ok=True)
        command(["mcopy", "-i", str(image), "::/TESTS.TXT", str(test_list)])
        declared = test_list.read_text(encoding="ascii")
        for _source, source_relative, image_relative, _kind in found:
            if image_relative not in declared or f"source {source_relative}" not in declared:
                raise RuntimeError(f"missing manifest mapping: {source_relative}")
        root_verifications = [
            ("RUNLIST.TXT", cases_file),
            ("RUNTEST.SH", Path(__file__).with_name("testharness.sh")),
            ("ABI.TXT", args.runtime.resolve() / "ABI.TXT"),
        ]
        if wasi_manifest_file:
            root_verifications.append(("WASIRUN.TXT", wasi_manifest_file))
            root_verifications.append(("WASIRUN.JSN", wasi_json_file))
        for root_file, source in root_verifications:
            extracted_root = destination / root_file
            extracted_root.unlink(missing_ok=True)
            command(["mcopy", "-i", str(image), f"::/{root_file}", str(extracted_root)])
            if source.read_bytes() != extracted_root.read_bytes():
                raise RuntimeError(f"{root_file} differs from the source")
        verify_runlist(cases_file, expected_files)
        verify_wasirun(wasi_manifest_file, expected_files, expected_directories)
        if reference_rows and log_root:
            report = ["path\tstatus\texit_status"]
            report.extend(f"{path}\t{status}\t{code}" for path, status, code in reference_rows)
            (log_root / "extracted-reference.tsv").write_text("\n".join(report) + "\n",
                                                               encoding="utf-8")
    print(f"FAT verified: {image} ({len(found)} artifacts readable and identical)")
    return 0


def verify_ext4(args, image, found, cases_file, wasi_manifest_file=None, wasi_json_file=None):
    with tempfile.TemporaryDirectory(prefix="kaleidos-ext4-verify-") as temporary:
        destination = Path(temporary)
        command(["debugfs", "-R", f"rdump / {destination}", str(image)])
        expected_files = {image_relative for _source, _source_relative, image_relative, _kind in found}
        expected_files.update({"TESTS.TXT", "RUNLIST.TXT", "RUNTEST.SH"})
        if wasi_manifest_file:
            expected_files.add("WASIRUN.TXT")
            expected_files.add("WASIRUN.JSON")
        actual_files = {path.relative_to(destination).as_posix()
                        for path in destination.rglob("*") if path.is_file()}
        if actual_files != expected_files:
            raise RuntimeError("ext4 file list differs: " + set_difference(expected_files, actual_files))
        for source, source_relative, image_relative, _kind in found:
            extracted = destination / image_relative
            original = source
            if extracted.stat().st_size != original.stat().st_size:
                raise RuntimeError(f"size changed in ext4 image: {image_relative}")
            command(["cmp", "--", str(original), str(extracted)])
            if args.run_native and image_relative.startswith("libctest/") and args.arch == "x86_64" and _kind == "ELF":
                run_directory = destination / ("run-" + source_relative.replace("/", "_"))
                run_directory.mkdir()
                if source_relative.endswith("/posix_stat"):
                    time.sleep(1.1 - (time.time() % 1))
                process = subprocess.run([str(extracted)], cwd=run_directory,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, timeout=30)
                if process.returncode:
                    raise RuntimeError(f"extracted Linux reference exited {process.returncode}: {source_relative}")
        declared = (destination / "TESTS.TXT").read_text(encoding="ascii")
        for _source, source_relative, image_relative, _kind in found:
            if image_relative not in declared or f"source {source_relative}" not in declared:
                raise RuntimeError(f"missing manifest mapping: {source_relative}")
        root_verifications = [
            ("RUNLIST.TXT", cases_file),
            ("RUNTEST.SH", Path(__file__).with_name("testharness.sh")),
            ("ABI.TXT", args.runtime.resolve() / "ABI.TXT"),
        ]
        if wasi_manifest_file:
            root_verifications.append(("WASIRUN.TXT", wasi_manifest_file))
            root_verifications.append(("WASIRUN.JSON", wasi_json_file))
        for root_file, source in root_verifications:
            if source.read_bytes() != (destination / root_file).read_bytes():
                raise RuntimeError(f"{root_file} differs from the source")
        verify_runlist(cases_file, expected_files)
        verify_wasirun(wasi_manifest_file, expected_files)
    print(f"ext4 verified: {image} ({len(found)} artifacts readable and identical)")
    return 0


def verify_runlist(manifest, expected_files):
    for line in manifest.read_text().splitlines():
        fields = line.split("|")
        if len(fields) != 9 or fields[4] not in expected_files:
            raise RuntimeError(f"invalid shell run manifest row: {line}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("pack", "verify"):
        sub = subparsers.add_parser(action)
        sub.add_argument("--input", type=Path, required=True, help="suite/isa artifact root")
        sub.add_argument("--suites", required=True, help="comma-separated suite names")
        sub.add_argument("--arch", required=True, help="one ISA, represented by this image")
        sub.add_argument("--abi", default="unknown", help="target runtime ABI label")
        sub.add_argument("--runtime", type=Path, required=True, help="staged root filesystem runtime")
        sub.add_argument("--fs", default="fat", help="disk filesystem: fat or ext4")
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
    except (OSError, RuntimeError, UnicodeError, ValueError, tarfile.TarError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
