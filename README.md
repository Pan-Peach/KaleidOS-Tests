# KaleidOS-Tests

KaleidOS-Tests pins upstream user-space suites and builds one self-contained test
root filesystem per ISA. A disk has one ISA and one declared target ABI; test
programs are placed directly in suite directories, with no second ISA directory.
Each disk includes a static BusyBox runtime, `/bin/sh`, the shared
`RUNTEST.SH` harness, `RUNLIST.TXT`, `TESTS.TXT`, and writable `work/` and
`logs/` directories. Linux x86_64 glibc dynamic loaders and libraries are copied
at their ELF interpreter and dependency paths. Picolibc test executables are
built against Picolibc itself.

The harness is only run when a user invokes it. Booting KaleidOS or starting
QEMU never runs tests. Linux results describe the Linux host kernel and do not
count as KaleidOS results. `chroot` is not a security sandbox: tests still
exercise the host Linux kernel and should be treated as untrusted programs.

## Sources and host prerequisites

From the KaleidOS repository root:

```sh
git submodule update --init tests/kaleidos-tests
git -C tests/kaleidos-tests submodule update --init \
  third_party/libc-test third_party/picolibc third_party/busybox
sh tests/kaleidos-tests/tools/prepare-openposix.sh
```

Common tools are Python 3, a C compiler for the selected target, `make`,
`readelf`, `ldd`, and e2fsprogs for ext4. Picolibc additionally needs Meson and
Ninja. Open POSIX needs Autoconf and Make. FAT additionally needs `dosfstools`
and `mtools`. Linux chroot execution requires root privileges, loop mounting,
and permission to bind `/dev` and mount `/proc`.

## Build one image

The Make interface selects one ISA and execution target:

```sh
make test-image ARCH=x86_64 TARGET=linux FS=ext4 SUITE=all
make test-image ARCH=rv64 TARGET=kaleid FS=fat SUITE=all
make test-image ARCH=x86_64 TARGET=linux FS=ext4 SUITE=libctest
```

Outputs are isolated by ISA, target, filesystem, ABI, and suite selection:

```text
build/tests/x86_64-linux-ext4-linux-gnu-all/test-x86_64.img
build/tests/rv64-kaleid-fat-kaleidos-posix-all/test-rv64.img
```

`FS=ext4` is the Linux default; `FS=fat` is the KaleidOS default. Both formats
use the same suite builders and run manifest. ext4 retains full upstream test
names. FAT applies 8.3 aliases only while packing. KaleidOS currently has no
ext4 provider, so its QEMU image uses FAT.

`SUITE=all` visits `libctest`, `picolibc`, `busybox`, then `openposix`. An
unavailable ISA adapter is recorded as `UNSUPPORTED` and displayed as `SKIP`.
A failed compiler/build step is recorded as `BUILD_FAIL`; if another suite
produced runnable tests, the image and status manifest are still written, but
the build command returns failure. If no runnable suite was produced, no
successful image is reported. The current adapter matrix is:

| Suite | x86_64 / Linux GNU | RV64 / Kaleid | RV32, AArch64, LoongArch64 |
| --- | --- | --- | --- |
| libc-test | supported by host `gcc` | supported by `riscv64-linux-gnu-gcc` (Linux GNU binaries) | requires the matching Linux cross compiler |
| Picolibc | supported, linked with Picolibc | `SKIP` (adapter unavailable) | `SKIP` (adapter unavailable) |
| BusyBox runtime/tests | supported, static GNU build | supported if cross compiler is installed; Linux GNU ABI | requires matching Linux cross compiler |
| Open POSIX | supported, Linux GNU | `SKIP` (adapter unavailable) | `SKIP` (adapter unavailable) |

`TARGET=kaleid` labels the intended guest environment; it does not change the
ABI of cross-compiled upstream programs. For example, RV64 libc-test and
BusyBox currently use a Linux GNU cross compiler, so they are not declared
KaleidOS-compatible. A KaleidOS POSIX toolchain/runtime is a prerequisite for
guest execution. No syscall is pre-checked to turn a real test failure into a
skip; only declared missing build adapters and explicit external prerequisites
are skipped.

In the current build environment, `x86_64-linux-gnu` produced all four suites;
`rv64-kaleidos-posix` produced libc-test and BusyBox while recording Picolibc
and Open POSIX as `UNSUPPORTED`/`SKIP`. RV32, AArch64, and LoongArch64 were not
built because their cross compilers are not installed here. The RV64 binaries
that did build are Linux GNU ABI programs, not KaleidOS test passes.

The run manifest records suite, binary ABI, libc, case name, wrapper, timeout,
build state, reason, and external prerequisites. Runtime status is `PASS`,
`FAIL`, `CRASH`, `TIMEOUT`, `SKIP`, `WARN`, or `BUILD_FAIL`. Captured case
output is printed under its result and saved with `RESULTS.TXT` in `logs/`.
Open POSIX return codes 2 are reported as `WARN`, and its upstream unsupported
or untested return codes 4/5 are `SKIP`.

## Run in a Linux chroot

Build first, then explicitly run the image. `ACTION=run` never rebuilds:

```sh
make test TARGET=linux ACTION=build ARCH=x86_64 FS=ext4 SUITE=all
make test TARGET=linux ACTION=run ARCH=x86_64 FS=ext4 SUITE=all
```

The run helper mounts the ext4 image, bind-mounts the basic `/dev/null`,
`zero`, `random`, `urandom`, and `tty` devices, mounts `/proc`, enters the
image's own static `/bin/sh`, and invokes `/RUNTEST.SH`. It does not
fall back to the host shell or extracted files. `sudo` is used for the mount
and chroot if needed. To select a suite or case directly:

```sh
sudo mkdir -p /mnt/kaleidos-tests
sudo mount -o loop,exec build/tests/x86_64-linux-ext4-linux-gnu-all/test-x86_64.img /mnt/kaleidos-tests
sudo chroot /mnt/kaleidos-tests /bin/sh /RUNTEST.SH --suite libctest
sudo chroot /mnt/kaleidos-tests /bin/sh /RUNTEST.SH --suite picolibc
sudo chroot /mnt/kaleidos-tests /bin/sh /RUNTEST.SH --suite busybox
sudo chroot /mnt/kaleidos-tests /bin/sh /RUNTEST.SH --suite openposix --case mmap_11-4
sudo chroot /mnt/kaleidos-tests /bin/sh /RUNTEST.SH
sudo umount /mnt/kaleidos-tests
```

These commands run tests only after the user enters them. Open POSIX is last in
the default `all` ordering because it contains the largest case set.

## Boot a KaleidOS test disk

This target builds the image, attaches it as a QEMU virtio block device, and
boots to the ordinary KaleidOS shell. It sends no serial commands and does not
run or terminate tests:

```sh
make qemu-test-image ARCH=rv64 TARGET=kaleid FS=fat SUITE=all
```

The expected image path is
`build/tests/rv64-kaleid-fat-kaleidos-posix-all/test-rv64.img`. The current
guest can use the existing FAT/block-device path to inspect files, subject to
the current boot configuration. Its current ksh is not a POSIX shell and
cannot interpret `RUNTEST.SH`; do not infer POSIX test support from the image
being mounted. When a POSIX shell is available, the intended manual command is:

```sh
sh /fat/RUNTEST.SH --suite libctest
```

No ext4 driver, POSIX syscall expansion, automatic test service, or boot-time
test hook is part of this repository change.

## Private submodule in GitHub Actions

The CoreTest, QEMU, and ArchTest workflow jobs initialize only public
`third_party/` modules and do not need credentials for this repository. The
separate compatibility workflow that builds user-space suites needs a
repository secret named `KALEIDOS_TESTS_TOKEN`. Use a fine-grained token with
read-only **Contents** access to the private `Pan-Peach/KaleidOS-Tests`
repository only. The source repository remains private.

## Image layout and upstream suites

Example Linux ext4 root:

```text
ABI.TXT  RUNTEST.SH  RUNLIST.TXT  TESTS.TXT
bin/{sh,busybox,cp,mkdir,rm,chmod,sed,timeout,...}
lib/  lib64/  etc/  dev/  proc/  tmp/  work/  logs/
libctest/  picolibc/  busybox/  openposix/
```

There is one image per ISA; suite files are flat within their suite directory.
The root `ABI.TXT` declares target ABI and filesystem, while `RUNLIST.TXT`
records each test program's actual compiler ABI and libc. They are distinct:
ISA equality does not imply ABI compatibility.

Included suites are libc-test, Picolibc Testsuite, Open POSIX Testsuite, and a
selected BusyBox testsuite subset (`cat`, `echo`, `cp`, `ls`, `rm`, `pwd`,
`printf`). Full LTP is deferred. Other candidate suites (OSComp basic syscall
tests, lmbench, UnixBench, iozone, Lua, LTP) are not built here. Upstream
licenses and notices remain in the pinned source repositories and packaged
source data.
