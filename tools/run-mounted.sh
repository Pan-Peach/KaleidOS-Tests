#!/bin/sh
set -eu

image=$1
logs=${2:-}
mountpoint=$(mktemp -d "${TMPDIR:-/tmp}/kaleidos-test-mount.XXXXXX")
cleanup() {
    if mountpoint -q "$mountpoint"; then
        if [ "$(id -u)" -eq 0 ]; then umount "$mountpoint"; else sudo umount "$mountpoint"; fi
    fi
    rmdir "$mountpoint"
}
trap cleanup EXIT HUP INT TERM

if [ "$(id -u)" -eq 0 ]; then
    mount -o "loop,exec,uid=$(id -u),gid=$(id -g),umask=022" "$image" "$mountpoint"
else
    sudo mount -o "loop,exec,uid=$(id -u),gid=$(id -g),umask=022" "$image" "$mountpoint"
fi
if [ -n "$logs" ]; then
    python3 "$mountpoint/RUNTEST.PY" --logs "$logs"
else
    python3 "$mountpoint/RUNTEST.PY"
fi
