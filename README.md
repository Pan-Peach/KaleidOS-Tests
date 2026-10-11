# KaleidOS-Tests

This repository pins upstream user-space suites and builds them into one FAT or ext4
image organized by suite and ISA. Every image includes the POSIX shell entry
`RUNTEST.SH`, its plain-text `RUNLIST.TXT`, per-case shell entries, and test
binaries/data. The same entry runs on Linux and on a guest with a working `sh`
and the required POSIX facilities. Runtime execution does not require Python,
JSON parsing, or archive extraction. Python remains a build-host dependency.
Results and per-case output go to `logs/` beside the entry; use `--logs DIR`
for a writable location outside the image.

## Included suites

- `libctest`: selected upstream libc-test cases, built for Linux. Each source
  test and its build status is listed in the run manifest.
- `picolibc`: upstream Meson test programs linked with Picolibc's Linux libc
  and Linux OS glue. The current adapter targets x86_64. It packages only
  tests Meson reported as passing in the build environment; the host harness
  executes them again from the image.
- `openposix`: the Open POSIX conformance, functional, and stress cases from
  LTP's `testcases/open_posix_testsuite` subtree. The source checkout uses
  blob filtering and sparse checkout so only that subtree is populated. The
  adapter builds it for x86_64 Linux and runs each generated `.run-test` case.
- `busybox`: upstream BusyBox binary and a curated set of upstream testsuite
  applets (`cat`, `echo`, `cp`, `ls`, `rm`, `pwd`, `printf`). The same selected
  tests are checked during the build and run from the image.

The full LTP kernel and system test collection remains deferred. Open POSIX is
packaged as its own suite; LTP's other tests are neither built nor copied into
the image. BusyBox's full testsuite is also not claimed: only the selected
applets above are included.

Picolibc reference configuration follows its upstream Meson options:
`tests=true`, `native-tests=false`, and `tests-enable-posix-io=false`. The
`native-tests=true` option tests the host libc and is not used. See the
[upstream build options](https://github.com/picolibc/picolibc/blob/main/doc/build.md).
BusyBox uses the upstream `testsuite/runtest` selector interface documented in
its [testsuite README](https://git.busybox.net/busybox/plain/testsuite/README).

## Build and run on Linux

From the KaleidOS repository root:

```sh
git submodule update --init tests/kaleidos-tests
git -C tests/kaleidos-tests submodule update --init third_party/libc-test third_party/picolibc third_party/busybox
sh tests/kaleidos-tests/tools/prepare-openposix.sh
make test TARGET=linux SUITE=all
make test TARGET=linux FS=ext4 SUITE=all
make test TARGET=linux ACTION=build FS=ext4 SUITE=all
make test TARGET=linux ACTION=run FS=ext4 SUITE=all
```

`ACTION=build` creates `build/tests/test-linux-<FS>-<SUITE>/tests.img`.
The Picolibc and BusyBox build adapters currently perform reference checks
while preparing their artifacts; disk execution is a separate action.
`ACTION=run` uses an existing image without rebuilding it;
`ACTION=all` is the default and performs both steps. The run action calls
`tools/run-mounted.sh`, which mounts the image on Linux and runs `RUNTEST.SH`.
If loop mounting is unavailable, it extracts the verified image to a temporary
directory and runs the same harness there. For a selected run without Make:

```sh
sh tests/kaleidos-tests/tools/run-mounted.sh build/tests/test-linux-ext4-all/tests.img build/tests/test-linux-ext4-all/logs ext4 --suite libctest,busybox
```

You can also mount the image yourself
and run `sh RUNTEST.SH --logs <directory>` directly. `FS=fat` is the
default; `FS=ext4` creates a native ext4
disk with full filenames. FAT requires mtools and dosfstools; ext4 requires
e2fsprogs. Both need loop mount permission for direct mounting. To mount and
inspect the default FAT disk manually:

```sh
sudo mkdir -p /mnt/kaleidos-tests
sudo mount -o "loop,exec,uid=$(id -u),gid=$(id -g),umask=022" build/tests/test-linux-fat-all/tests.img /mnt/kaleidos-tests
cd /mnt/kaleidos-tests
sh RUNTEST.SH --logs "$HOME/kaleidos-test-logs"
cat "$HOME/kaleidos-test-logs/RESULTS.TXT"
cd -
sudo umount /mnt/kaleidos-tests
```

For the ext4 variant:

```sh
sudo mount -o loop,exec build/tests/test-linux-ext4-all/tests.img /mnt/kaleidos-tests
```

ext4 preserves the full artifact filenames and file modes.

Select suites with `SUITE=libctest`, `SUITE=picolibc`, `SUITE=openposix`,
`SUITE=busybox`, or a comma-separated combination. `SUITE=all` runs libctest,
picolibc, busybox, then openposix; Open POSIX is placed last for an explicit
suite list too. Missing compilers or build
tools are reported as errors; Open POSIX also requires Autoconf. The run
harness prints each suite between
separators, colors PASS green, FAIL red, and WARN/SKIP yellow on interactive
terminals, prints each test's captured output beneath its result, and reports
Open POSIX exit codes for unsupported and untested cases as `SKIP`, unresolved
cases as `WARN`, and incompatible ISAs as `SKIP`. Cases requiring unavailable
host privileges are also skipped; a skip is never counted as a pass. Use
`--color always` or `--color never` when running `RUNTEST.SH` directly to
override terminal detection.

## KaleidOS image

`make test TARGET=kaleid ARCH=rv64 SUITE=libctest` builds an ISA-specific FAT
image containing **the same shell harness** and boots it as KaleidOS's FAT root
disk. Once a real POSIX shell and the necessary userspace APIs are available,
the guest entry is:

```sh
sh /fat/RUNTEST.SH --isa rv64 --logs /local/test-logs
```

This is the intended guest invocation, not a command supported by today's
`ksh`: the current monitor has no script interpreter, and the POSIX personality
still lacks the general file/process facilities needed by a shell. This change
packages the orchestration; it does not implement those kernel/user-space APIs.
The shell interpreter itself must be provided by the system. ISA matching also
does not prove ELF/libc/syscall compatibility. BusyBox, Picolibc, and Open POSIX
currently have x86_64 Linux build adapters; they are skipped on RV64. Linux
results do not establish KaleidOS compatibility.

The harness checks ISA, required utility availability and, on Linux, the known
privileged cases before starting tests. It requires `sh`, `cp`, `chmod`, `mkdir`,
`rm`, `sed`, a writable work directory (`TMPDIR`, default `/tmp`), and `timeout`
with `-k` support to bound blocking cases. BusyBox also requires `ln` and the
utilities used by its upstream tests. Missing launch prerequisites are reported
as SKIP; upstream unsupported/untested results remain SKIP, not PASS. A run in
which no case executes returns 2. Actual syscall behavior is assessed by the
cases, rather than inferred from the OS name.

Use `--suite libctest,busybox`, `--case mmap_11-4`, or `--color always` to select
cases and output style. `uname -m` detects ISA when available; `--isa` supplies
it on a system without `uname`.

## Image layout and licenses

FAT images use readable, collision-checked 8.3 filenames because KaleidOS
FatFs disables long filenames. ext4 images keep full filenames for Linux host
use. Generated shell entries and flattened runtime assets use short names in
both formats. Both include `TESTS.TXT` mappings and a UTF-8, LF-terminated `RUNLIST.TXT`.
Its pipe-separated fields are `suite|isa|name|entry|timeout|build_status|reason|requires`.
Each entry is a shell file with safely quoted arguments and environment values.
Open POSIX executables and BusyBox testsuite assets are unpacked on the build
host and assigned flat names compatible with FAT 8.3; the BusyBox entry restores
its required working tree before execution. Run reports use
`suite|isa|name|status|detail`, with output in `case-N.log` in report order.
Older Python-runner images must be rebuilt before using this entry.
Build products stay under KaleidOS `build/` and do not modify upstream checkouts.

Upstream copyright and license files remain in each source submodule. BusyBox
binary redistribution must follow its GPL-2.0-or-later terms for the exact
configuration. The selected BusyBox testsuite files retain their upstream
notices in the packaged files.
