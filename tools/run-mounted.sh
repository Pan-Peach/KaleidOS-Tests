#!/bin/sh
set -eu

image=$1
logs=${2:-}
filesystem=${3:-fat}
if [ "$#" -ge 3 ]; then shift 3; else set --; fi
mountpoint=$(mktemp -d "${TMPDIR:-/tmp}/kaleidos-test-mount.XXXXXX")
mounted=0
cleanup() {
    if [ "$mounted" -eq 1 ]; then
        if [ "$(id -u)" -eq 0 ]; then umount "$mountpoint"; else sudo umount "$mountpoint"; fi
    fi
    rm -rf "$mountpoint"
}
trap cleanup EXIT HUP INT TERM

if [ "$filesystem" = fat ]; then
    if [ "$(id -u)" -eq 0 ]; then
        mount -t vfat -o "loop,exec,uid=$(id -u),gid=$(id -g),umask=022" "$image" "$mountpoint" && mounted=1 || true
    else
        sudo mount -t vfat -o "loop,exec,uid=$(id -u),gid=$(id -g),umask=022" "$image" "$mountpoint" && mounted=1 || true
    fi
elif [ "$filesystem" = ext4 ]; then
    if [ "$(id -u)" -eq 0 ]; then
        mount -t ext4 -o loop,exec "$image" "$mountpoint" && mounted=1 || true
    else
        sudo mount -t ext4 -o loop,exec "$image" "$mountpoint" && mounted=1 || true
    fi
else
    echo "unsupported filesystem '$filesystem'; use fat or ext4" >&2
    exit 2
fi

if [ "$mounted" -eq 0 ]; then
    echo "Loop mount unavailable; extracting the $filesystem image to a temporary directory." >&2
    if [ "$filesystem" = fat ]; then
        mcopy -s -i "$image" '::/*' "$mountpoint"
    else
        debugfs -R "rdump / $mountpoint" "$image" >/dev/null
    fi
fi
if [ ! -r "$mountpoint/RUNTEST.SH" ]; then
    echo 'Image has no RUNTEST.SH; rebuild it with ACTION=build before running.' >&2
    exit 2
fi
if [ -n "$logs" ]; then
    sh "$mountpoint/RUNTEST.SH" --logs "$logs" "$@"
else
    sh "$mountpoint/RUNTEST.SH" "$@"
fi
