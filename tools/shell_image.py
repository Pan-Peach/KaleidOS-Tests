"""Build the shell run manifest and unpack runtime assets on the build host."""

import json
from pathlib import Path
import re
import shlex
import tarfile


def prepare(root, suites, selected, temporary):
    paths = {original: image for _source, original, image, _kind in selected}
    generated = []
    manifest = []
    archives = set()

    def asset(suite, isa, name, contents, kind="DATA", original=None):
        relative = f"{suite}/{isa}/{name}"
        source = temporary / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(contents)
        if kind == "ELF" or name.endswith(".SH"):
            source.chmod(0o755)
        generated.append((source, original or relative, relative, kind))
        return relative

    def image_path(relative):
        return '"$TEST_ROOT/"' + shlex.quote(relative)

    def case(suite, isa, name, commands, timeout=30, status="BUILT", reason="-", requires="-"):
        number = len(manifest) + 1
        wrapper = asset(suite, isa, f"C{number:07d}.SH",
                        ("#!/bin/sh\nset -eu\n" + commands + "\n").encode(),
                        original=f"{name} (shell entry)")
        reason = re.sub(r"[|\r\n]+", " ", reason or "-")
        fields = [suite, isa, name, wrapper, str(max(1, int(timeout))),
                  status or "BUILT", reason, requires]
        if any("|" in field or "\n" in field or "\r" in field for field in fields):
            raise RuntimeError(f"invalid shell manifest field: {name}")
        manifest.append("|".join(fields))

    for suite in [s for s in suites if s != "openposix"] + (["openposix"] if "openposix" in suites else []):
        for isa_root in sorted(p for p in (root / suite).iterdir() if p.is_dir()):
            isa = isa_root.name
            sidecar = isa_root / ".runlist.json"
            if not sidecar.is_file():
                raise RuntimeError(f"missing execution manifest: {sidecar}")
            data = json.loads(sidecar.read_text())
            if suite == "busybox":
                archives.add(data["tests"])
                setup = ['mkdir -p bin testsuite',
                         f'cp {image_path(paths[data["binary"]])} bin/busybox',
                         f'cp {image_path(paths[data["config"]])} bin/.config',
                         f'cp {image_path(paths[data["echo"]])} bin/echo-ne',
                         'chmod +x bin/busybox bin/echo-ne']
                with tarfile.open(root / data["tests"]) as archive:
                    for number, member in enumerate(sorted(archive.getmembers(), key=lambda m: m.name)):
                        if not member.isfile():
                            continue
                        destination = Path(member.name)
                        if destination.is_absolute() or ".." in destination.parts:
                            raise RuntimeError(f"unsafe BusyBox archive path: {member.name}")
                        relative = asset(suite, isa, f"B{number:07d}.DAT", archive.extractfile(member).read(),
                                         original=f"busybox/{isa}/{member.name}")
                        setup.extend([f"mkdir -p {shlex.quote(str(destination.parent))}",
                                      f"cp {image_path(relative)} {shlex.quote(member.name)}",
                                      f"chmod {member.mode & 0o777:o} {shlex.quote(member.name)}"])
                setup += ['TEST_WORK=$(pwd)', 'export TEST_WORK',
                          'ln -s "$TEST_WORK/bin/busybox" testsuite/busybox',
                          'PATH="$TEST_WORK/bin:$PATH"; export PATH',
                          'tsdir="$TEST_WORK/testsuite"; bindir="$TEST_WORK/bin"',
                          'ECHO="$bindir/echo-ne"; SKIP_INTERNET_TESTS=1',
                          'export tsdir bindir ECHO SKIP_INTERNET_TESTS', 'cd testsuite',
                          "exec sh ./runtest " + " ".join(map(shlex.quote, data["applets"]))]
                case(suite, isa, "busybox/testsuite (" + ", ".join(data["applets"]) + ")",
                     "\n".join(setup), timeout=180)
            elif suite == "openposix":
                for item in data:
                    archives.add(item["source"])
                    with tarfile.open(root / item["source"]) as archive:
                        members = {m.name: m for m in archive.getmembers() if m.isfile()}
                        tests = sorted(name for name in members if name.endswith(".run-test"))
                        if not tests:
                            raise RuntimeError("Open POSIX archive has no .run-test binaries")
                        license_name = "open_posix_testsuite/COPYING"
                        if license_name in members:
                            asset(suite, isa, "COPYING.DAT", archive.extractfile(members[license_name]).read(),
                                  original=f"openposix/{isa}/{license_name}")
                        for number, name in enumerate(tests):
                            relative = asset(suite, isa, f"O{number:07d}.ELF",
                                             archive.extractfile(members[name]).read(), "ELF",
                                             original=f"openposix/{isa}/{name}")
                            args_name = name.removesuffix(".run-test") + ".args"
                            args = shlex.split(archive.extractfile(members[args_name]).read().decode()) if args_name in members else []
                            logical = "openposix/" + name.split("/", 1)[1].removesuffix(".run-test")
                            requires = "-"
                            if logical.endswith("functional/timers/clocks/clocks_twopsetclock"):
                                requires = "clock-set"
                            elif logical.endswith("conformance/interfaces/pthread_rwlock_unlock/pthread_rwlock_unlock_3-1"):
                                requires = "fifo"
                            commands = f"cp {image_path(relative)} ./case.elf\nchmod +x ./case.elf\nexec ./case.elf " + " ".join(map(shlex.quote, args))
                            case(suite, isa, logical, commands, item.get("timeout", 60), requires=requires)
            else:
                for item in data:
                    relative = paths.get(item.get("source"))
                    status = item.get("status", "BUILT")
                    if not relative and status == "BUILT":
                        raise RuntimeError(f"missing binary: {item}")
                    commands = "exit 2"
                    if relative:
                        commands = f"cp {image_path(relative)} ./case.elf\nchmod +x ./case.elf\n"
                        for key, value in item.get("env", {}).items():
                            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                                raise RuntimeError(f"invalid environment key: {key}")
                            commands += f"{key}={shlex.quote(str(value))}" + (':"$PATH"' if key == "PATH" else "") + f"\nexport {key}\n"
                        commands += "exec ./case.elf " + " ".join(shlex.quote(str(arg)) for arg in item.get("args", []))
                    case(suite, isa, item.get("name", item.get("source", suite)), commands,
                         item.get("timeout", 30), status, item.get("reason", "-"))
    if not manifest:
        raise RuntimeError("selected artifacts have no execution entries")
    cases_file = temporary / "RUNLIST.TXT"
    cases_file.write_text("\n".join(manifest) + "\n")
    result = [entry for entry in selected if entry[1] not in archives] + generated
    image_names = [entry[2] for entry in result]
    if len(set(image_names)) != len(image_names):
        raise RuntimeError("generated shell assets collide with image filenames")
    return result, cases_file
