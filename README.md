# KaleidOS-Tests

This repository holds upstream user-space test suites and builds their selected
programs into one FAT disk image. It does not contain a guest runner or make
KaleidOS claim a PASS from a Linux reference run.

## Current state

`libctest` is migrated from KaleidOS at the original upstream revision pinned
by the `third_party/libc-test` gitlink. Its selected tests, diagnostics, and native
Linux/Windows build runner are retained. The first image builder supports Linux
ELF builds for `x86_64`, `rv64`, `rv32`, `aarch64`, and `loongarch64`; cross builds
require a Linux-targeting compiler for the requested ISA. A Linux x86_64 image
build can be checked with the native reference runner.

Picolibc, Open POSIX Testsuite, and BusyBox are reserved suite names but are not
yet integrated. They fail with an explicit message when selected. `SUITE=all`
therefore fails until each selected suite is implemented; nothing is skipped.

## Build and verify

From KaleidOS, initialize this repository and its pinned libc-test source, then
run:

```sh
make test-image ARCH=rv64 SUITE=libctest
make test-image TARGETS=rv64,x86_64 SUITE=libctest
make test-image-verify ARCH=rv64 SUITE=libctest
make test-reference-linux SUITE=libctest
```

For a host-only image, use `TARGETS=x86_64`. Build products stay in KaleidOS'
`build/` tree. The image has `suite/isa/` directories and a short `TESTS.TXT`
index. KaleidOS FatFs disables long filenames, so the image uses readable,
collision-checked 8.3 names. `TESTS.TXT` maps each image path to the original
test name.

Required image tools are `mkfs.vfat`, `mmd`, `mcopy`, `mdir`, and `cmp`.
Compilers must match their requested ISA and Linux ABI. A missing compiler is a
build error.

## QEMU

`make qemu-test-image ARCH=rv64 SUITE=libctest` uses `tests.img` as the boot
FAT root disk. KaleidOS init mounts the first discovered block device at `/fat`;
the existing init path does not mount a second data disk. Use the monitor's
`cat /fat/TESTS.TXT` to inspect the image. Executing a program still depends on
the guest ISA, executable ABI, and implemented syscalls.

## Licenses

Upstream copyright and license files remain in each source submodule. BusyBox
binary redistribution must follow its GPL-2.0-or-later terms, including the
corresponding source-offer and license obligations for the exact configured
build. The BusyBox testsuite may contain separately licensed test material;
its notices must be retained when that suite is added.
