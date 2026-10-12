#!/bin/sh
set -eu

image=$1
logs=$2
filesystem=$3
shift 3
if [ "$filesystem" != ext4 ]; then
    echo 'Linux chroot execution requires FS=ext4; FAT remains available for KaleidOS.' >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    exec sudo "$0" "$image" "$logs" "$filesystem" "$@"
fi

mountpoint=$(mktemp -d "${TMPDIR:-/tmp}/kaleidos-test-chroot.XXXXXX")
bound_devices=
proc_mounted=0
root_mounted=0
logs_mounted=0
cleanup() {
    set +e
    if [ "${logs_mounted:-0}" -eq 1 ]; then umount "$mountpoint/logs" && logs_mounted=0; fi
    if [ "$proc_mounted" -eq 1 ]; then umount "$mountpoint/proc" && proc_mounted=0; fi
    for device in $bound_devices; do
        umount "$device" 2>/dev/null || umount -l "$device" 2>/dev/null
    done
    if [ "$root_mounted" -eq 1 ]; then
        umount "$mountpoint" 2>/dev/null || umount -l "$mountpoint" 2>/dev/null
        [ "$?" -ne 0 ] || root_mounted=0
    fi
    if [ "$root_mounted" -eq 0 ]; then rm -rf "$mountpoint";
    else echo "warning: leaving mounted chroot tree at $mountpoint for manual cleanup" >&2; fi
}
trap cleanup EXIT HUP INT TERM

mount -t ext4 -o loop,exec "$image" "$mountpoint"
root_mounted=1
for device in null zero random urandom tty; do
    mount --bind "/dev/$device" "$mountpoint/dev/$device"
    bound_devices="$mountpoint/dev/$device $bound_devices"
done
mount -t proc proc "$mountpoint/proc"
proc_mounted=1
mkdir -p "$logs"
mount --bind "$logs" "$mountpoint/logs"
logs_mounted=1

cd /
chroot "$mountpoint" /bin/sh /RUNTEST.SH --logs /logs "$@"
