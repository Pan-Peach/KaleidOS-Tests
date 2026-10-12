#!/bin/sh
# Shared disk runner. Python is used to prepare the disk, never to run this file.
set -u

usage() {
    echo 'Usage: sh RUNTEST.SH [--suite NAME[,NAME]] [--case TEXT] [--isa ISA] [--abi ABI] [--logs DIR] [--color auto|always|never]'
}
suites= case_filter= isa= abi= logs= color=auto
while [ "$#" -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --suite|--case|--isa|--abi|--logs|--color)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            case "$1" in
                --suite) suites="${suites:+$suites,}$2" ;;
                --case) case_filter=$2 ;;
                --isa) isa=$2 ;;
                --abi) abi=$2 ;;
                --logs) logs=$2 ;;
                --color) color=$2 ;;
            esac
            shift 2 ;;
        *) usage >&2; exit 2 ;;
    esac
done
case "$color" in auto|always|never) ;; *) usage >&2; exit 2 ;; esac
case "$0" in */*) script_dir=${0%/*} ;; *) script_dir=. ;; esac
TEST_ROOT=$(CDPATH= cd "$script_dir" && pwd) || exit 2
export TEST_ROOT
[ -r "$TEST_ROOT/RUNLIST.TXT" ] || { echo 'Missing RUNLIST.TXT; rebuild this image.' >&2; exit 2; }
image_abi=$(sed -n 's/^abi=//p' "$TEST_ROOT/ABI.TXT" 2>/dev/null | sed -n '1p')
image_arch=$(sed -n 's/^arch=//p' "$TEST_ROOT/ABI.TXT" 2>/dev/null | sed -n '1p')
[ -n "$abi" ] || abi=$image_abi
[ -n "$abi" ] || { echo 'Missing ABI.TXT abi field; rebuild this image.' >&2; exit 2; }
PATH=/bin:/usr/bin
export PATH
if [ -z "$isa" ]; then
    machine=$(uname -m 2>/dev/null || echo unknown)
    case "$machine" in
        x86_64|amd64) isa=x86_64 ;;
        riscv64|rv64) isa=rv64 ;;
        riscv32|rv32) isa=rv32 ;;
        aarch64|arm64) isa=aarch64 ;;
        *) echo "Unknown ISA '$machine'; specify --isa." >&2; exit 2 ;;
    esac
fi

green= red= yellow= reset=
if [ "$color" = always ] || { [ "$color" = auto ] && [ -t 1 ] && [ "${TERM:-}" != dumb ] && [ -z "${NO_COLOR:-}" ]; }; then
    green=$(printf '\033[32m'); red=$(printf '\033[31m')
    yellow=$(printf '\033[33m'); reset=$(printf '\033[0m')
fi
# A finite deadline is required before starting potentially blocking upstream tests.
missing=
for tool in sh cp chmod mkdir rm sed timeout; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -z "$missing" ] && ! timeout -k 1 1 sh -c 'exit 0' >/dev/null 2>&1; then
    missing=' timeout with -k support'
fi
for tool in mkdir rm; do
    command -v "$tool" >/dev/null 2>&1 || { echo "Harness requires $tool." >&2; exit 2; }
done
umask 077
attempt=0
while :; do
    work=${TMPDIR:-/tmp}/kaleidos-tests.$$.$attempt
    mkdir "$work" 2>/dev/null && break
    attempt=$((attempt + 1))
    [ "$attempt" -lt 100 ] || { echo 'Cannot create a private work directory.' >&2; exit 2; }
done
work=$(CDPATH= cd "$work" && pwd) || exit 2
trap 'rm -rf "$work"' 0
trap 'exit 130' HUP INT TERM
logs=${logs:-$TEST_ROOT/logs}
mkdir -p "$logs" || { echo 'Use --logs DIR to select a writable results directory.' >&2; exit 2; }
logs=$(CDPATH= cd "$logs" && pwd) || exit 2
report=$logs/RESULTS.TXT
printf 'suite|isa|name|status|detail\n' > "$report" || exit 2

# On Linux these two cases need privileges, and one mishandles EPERM upstream.
# Unknown privileges on another OS are left for the test itself to assess.
caps=
if [ -r /proc/self/status ]; then
    while read -r key value rest; do
        [ "$key" != CapEff: ] || { caps=$value; break; }
    done < /proc/self/status
fi
capability() {
    [ -n "$caps" ] || return 2
    [ "$((0x$caps & (1 << $1)))" -ne 0 ]
}
fifo_limit=$(ulimit -r 2>/dev/null || echo unknown)

suite= passed=0 failed=0 skipped=0 total_failed=0 count=0 executed=0
summary() {
    [ -n "$suite" ] || return
    printf '%s\nSuite %s: %s passed, %s failed, %s warnings/skips\n%s\n' \
        '--------------------------------------------------------' "$suite" "$passed" "$failed" "$skipped" \
        '--------------------------------------------------------'
}
while IFS='|' read -r current required_abi libc name wrapper deadline built reason requires; do
    [ -n "$current" ] || continue
    if [ -n "$suites" ]; then
        case ",$suites," in *",$current,"*) ;; *) continue ;; esac
    fi
    case "$name" in *"$case_filter"*) ;; *) continue ;; esac
    if [ "$current" != "$suite" ]; then
        summary
        suite=$current; passed=0; failed=0; skipped=0
        printf '\n================== SUITE: %s ==================\n' "$suite"
    fi
    count=$((count + 1))
    status= detail= output=
    if [ "$built" = UNSUPPORTED ]; then
        status=SKIP; detail=$reason
    elif [ "$built" = BUILD_FAIL ]; then
        status=BUILD_FAIL; detail=$reason
    elif [ "$image_arch" != "$isa" ]; then
        status=SKIP; detail="image requires ISA $image_arch; running $isa"
    elif [ "$required_abi" != "$abi" ]; then
        status=SKIP; detail="requires ABI $required_abi; image ABI is $abi ($libc)"
    elif [ "$built" != BUILT ]; then
        status=FAIL; detail="build $built: $reason"
    elif [ -n "$missing" ]; then
        status=SKIP; detail="missing runtime tools:$missing"
    elif [ "$requires" = clock-set ] && capability 25; then
        :
    elif [ "$requires" = clock-set ] && [ -n "$caps" ]; then
        status=SKIP; detail='requires CAP_SYS_TIME to set CLOCK_REALTIME'
    elif [ "$requires" = fifo ] && ! capability 23 && [ -n "$caps" ]; then
        case "$fifo_limit" in
            unlimited) ;;
            ''|*[!0-9]*) ;;
            *) if [ "$fifo_limit" -lt 4 ]; then
                   status=SKIP; detail='requires permission to use SCHED_FIFO priorities'
               fi ;;
        esac
    fi
    if [ "$suite" = busybox ] && ! command -v ln >/dev/null 2>&1; then
        status=SKIP; detail='missing runtime tool: ln'
    fi
    if [ -z "$status" ]; then
        case_dir=$work/$count
        output=$logs/case-$count.log
        : > "$output" || exit 2
        if mkdir "$case_dir"; then
            printf 'RUN     %s\n' "$name"
            (cd "$case_dir" && timeout -k 2 "$deadline" sh "$TEST_ROOT/$wrapper") < /dev/null > "$output" 2>&1
            code=$?
            executed=$((executed + 1))
            detail="exit $code"
            status=FAIL
            if [ "$code" -eq 0 ]; then status=PASS
            elif [ "$code" -eq 124 ] || [ "$code" -eq 137 ]; then status=TIMEOUT; detail="timeout after ${deadline}s"
            elif [ "$code" -ge 128 ]; then status=CRASH; detail="signal $((code - 128))"
            elif [ "$suite" = openposix ]; then
                case "$code" in 2) status=WARN ;; 4|5) status=SKIP ;; esac
            fi
            rm -rf "$case_dir"
        else
            status=FAIL; detail='cannot create case work directory'
        fi
    fi
    case "$status" in
        PASS) paint=$green; passed=$((passed + 1)) ;;
        SKIP|WARN) paint=$yellow; skipped=$((skipped + 1)) ;;
        *) paint=$red; failed=$((failed + 1)); total_failed=$((total_failed + 1)) ;;
    esac
    printf '%s%-7s%s %s (%s)\n' "$paint" "$status" "$reset" "$name" "$detail"
    [ -z "$output" ] || sed 's/^/        /' "$output"
    printf '%s|%s|%s|%s|%s\n' "$suite" "$required_abi" "$name" "$status" "$detail" >> "$report" || exit 2
done < "$TEST_ROOT/RUNLIST.TXT"
summary
printf '\nResults: %s\n' "$report"
if [ "$count" -eq 0 ]; then echo 'No matching test cases.' >&2; exit 2; fi
[ "$total_failed" -eq 0 ] || exit 1
if [ "$executed" -eq 0 ]; then echo 'No test case could run on this platform.' >&2; exit 2; fi
exit 0
