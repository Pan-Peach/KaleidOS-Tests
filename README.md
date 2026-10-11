# KaleidOS-Tests

This repository pins upstream user-space suites and builds them into one FAT
image organized by suite and ISA. The image includes `RUNTEST.PY`, which detects
the Linux host ISA and runs only compatible packaged tests. It writes
`RESULTS.TXT` to the mounted image when the filesystem is writable; pass
`--logs DIR` to save a host-side copy and per-test output.

## Included suites

- `libctest`: selected upstream libc-test cases, built for Linux. Each source
  test and its build status is listed in the run manifest.
- `picolibc`: upstream Meson test programs linked with Picolibc's Linux libc
  and Linux OS glue. The current adapter targets x86_64. It packages only
  tests Meson reported as passing in the build environment; the host harness
  executes them again from the image.
- `busybox`: upstream BusyBox binary and a curated set of upstream testsuite
  applets (`cat`, `echo`, `cp`, `ls`, `rm`, `pwd`, `printf`). The same selected
  tests are checked during the build and run from the image.

Open POSIX Testsuite and LTP are deferred. They are too large for this initial
host workflow; their sources are not initialized and `SUITE=all` does not
include them. BusyBox's full testsuite is also not claimed: only the selected
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
make test TARGET=linux SUITE=all
```

The command builds `build/tests/test-linux-all/tests.img`, mounts it on the
Linux host, and runs the packaged harness. It requires GCC, Python 3, Meson,
Ninja, mtools, dosfstools, and permission to mount a loop image. To mount and
inspect the disk manually:

```sh
sudo mkdir -p /mnt/kaleidos-tests
sudo mount -o "loop,exec,uid=$(id -u),gid=$(id -g),umask=022" build/tests/test-linux-all/tests.img /mnt/kaleidos-tests
cd /mnt/kaleidos-tests
python3 RUNTEST.PY --logs "$HOME/kaleidos-test-logs"
cat RESULTS.TXT
cd -
sudo umount /mnt/kaleidos-tests
```

Select suites with `SUITE=libctest`, `SUITE=picolibc`, `SUITE=busybox`, or a
comma-separated combination. Missing compilers or build tools are reported as
errors. The run harness reports incompatible ISAs as `SKIP` and test failures
as `FAIL`; a skip is never counted as a pass.

## KaleidOS image

`make test TARGET=kaleid ARCH=rv64 SUITE=libctest` builds an ISA-specific FAT
image and boots it as KaleidOS's FAT root disk. The current guest does not
provide Python or enough POSIX/ELF support to run these upstream suites
automatically. Guest execution remains manual and limited by the capabilities
listed in KaleidOS `STATUS.md`; Linux host results do not establish KaleidOS
compatibility.

## Image layout and licenses

The FAT image uses readable, collision-checked 8.3 filenames because KaleidOS
FatFs disables long filenames. `TESTS.TXT` maps image paths to upstream test
names, while `RUNLIST.TXT` contains the machine-readable harness plan. Build
products stay under KaleidOS `build/` and do not modify upstream checkouts.

Upstream copyright and license files remain in each source submodule. BusyBox
binary redistribution must follow its GPL-2.0-or-later terms for the exact
configuration. The selected BusyBox testsuite files retain their upstream
notices in the packaged archive.
