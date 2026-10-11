#!/bin/sh
set -eu

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
source="$repo/third_party/open_posix_testsuite"
url=https://github.com/linux-test-project/ltp.git
pin=$(git -C "$repo" ls-files -s -- third_party/open_posix_testsuite | awk 'NR == 1 { print $2 }')
if [ -z "$pin" ]; then
	echo "third_party/open_posix_testsuite is not pinned in the test repository" >&2
	exit 1
fi

git -C "$repo" submodule init third_party/open_posix_testsuite

if [ ! -e "$source/.git" ]; then
	git clone --filter=blob:none --no-checkout "$url" "$source"
fi

git -C "$source" sparse-checkout init --cone
git -C "$source" sparse-checkout set testcases/open_posix_testsuite
git -C "$source" checkout --detach "$pin"
git -C "$repo" submodule absorbgitdirs third_party/open_posix_testsuite

if [ ! -f "$source/testcases/open_posix_testsuite/Makefile" ]; then
	echo "Open POSIX source checkout is incomplete" >&2
	exit 1
fi

echo "Open POSIX source ready at $source/testcases/open_posix_testsuite"
