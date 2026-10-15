#!/usr/bin/env bash
# Builds the MPP libraries that rk_ab.sh can load, on an Ubuntu 22.04 arm64
# machine with glibc 2.35 like the RK board (the Linux VM on the Mac). Do not
# build on the RK: it is the robot's motion-control board.
#
#   bash deploy/rk_sender/build_mpp.sh [output-dir, default .local/rk-sender]
#
#   <output-dir>/mpp-base/librockchip_mpp.so.1  upstream MPP c3b2ece9 (2024-11), about the board's version
#   <output-dir>/mpp-fix/librockchip_mpp.so.1   the same plus mpp-async-wait-fix.patch (upstream 321a8ae1)
#   <output-dir>/mpp-pkt/librockchip_mpp.so.1   the same plus mpp-pkt-poll-fix.patch
#   <output-dir>/mpp-BUILD.txt                  source, patches, toolchain and SHA-256 of each
#
# Needs git, gcc, g++, make and network access to GitHub; without cmake it also
# needs python3-venv and PyPI (cmake is then installed into a local venv). Work
# files go to $MPP_WORK (default ~/mppbuild). An interrupted run continues where
# it stopped when run again.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out=${1:-.local/rk-sender}
mkdir -p "$out"
out=$(cd "$out" && pwd)
work=${MPP_WORK:-$HOME/mppbuild}
commit=c3b2ece9c29c0cd594c9ad0ac36235afe438b51e
flags='-fPIC -DARMLINUX -Dlinux'  # as MPP's build/linux/aarch64 toolchain file
variants='base fix pkt'
[ "$(uname -m)" = aarch64 ] || { echo "build on arm64 (an Ubuntu 22.04 arm64 machine)" >&2; exit 2; }
mkdir -p "$work"
cd "$work"
if ! command -v cmake >/dev/null; then
  [ -x venv/bin/cmake ] || { python3 -m venv venv && venv/bin/pip install -q cmake; }
  PATH=$work/venv/bin:$PATH
fi

patch_of() {  # patch_of <variant>: the patch file it adds, or nothing
  case "$1" in
    fix) echo "$here/mpp-async-wait-fix.patch" ;;
    pkt) echo "$here/mpp-pkt-poll-fix.patch" ;;
  esac
}

for v in $variants; do
  if ! git -C "src-$v" rev-parse -q --verify HEAD >/dev/null 2>&1; then  # also after an interrupted fetch
    rm -rf "src-$v" "build-$v"
    git init -q "src-$v"
    git -C "src-$v" fetch -q --depth 1 https://github.com/rockchip-linux/mpp "$commit"
    git -C "src-$v" checkout -q FETCH_HEAD
  fi
  patch=$(patch_of "$v")
  if [ -n "$patch" ] && ! git -C "src-$v" apply --reverse --check "$patch" 2>/dev/null; then
    git -C "src-$v" apply "$patch"
    # committed, so that the version line MPP embeds (from git at configure time) names the patch
    git -C "src-$v" -c user.name=build_mpp.sh -c user.email=build@localhost commit -q -a \
      -m "$(basename "$patch" .patch)"
  fi
  if [ ! -f "build-$v/Makefile" ]; then
    cmake -S "src-$v" -B "build-$v" -G 'Unix Makefiles' -DCMAKE_BUILD_TYPE=Release -DBUILD_TEST=OFF \
      -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DCMAKE_C_FLAGS="$flags" -DCMAKE_CXX_FLAGS="$flags" \
      > "build-$v.cmake.log" 2>&1
  fi
  echo "[mpp] building $v (log: $work/build-$v.make.log)"
  make -C "build-$v" -j"$(nproc)" rockchip_mpp > "build-$v.make.log" 2>&1
  mkdir -p "$out/mpp-$v"
  cp "build-$v/mpp/librockchip_mpp.so.0" "$out/mpp-$v/librockchip_mpp.so.1"
done

{
  echo "built $(date -u +%FT%TZ) on $(uname -m); $(ldd --version | head -n 1); $(gcc --version | head -n 1)"
  echo "source: https://github.com/rockchip-linux/mpp $commit"
  for v in $variants; do
    patch=$(patch_of "$v")
    if [ -n "$patch" ]; then
      echo "mpp-$v adds: $(sha256sum "$patch" | cut -c1-16)  $(basename "$patch")"
    fi
  done
  (cd "$out" && for v in $variants; do sha256sum "mpp-$v/librockchip_mpp.so.1"; done)
} > "$out/mpp-BUILD.txt"
cat "$out/mpp-BUILD.txt"
