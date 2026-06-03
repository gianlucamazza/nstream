#!/usr/bin/env bash
# Build the Arch package from the current git HEAD, offline.
#
# While the GitHub repo is private (Fase 2 publication paused) the PKGBUILD `source`
# URL can't be fetched, so we hand makepkg a tarball produced from HEAD with the
# exact name it expects (`nstream-<ver>.tar.gz`); makepkg then skips the download.
# The package version is read from src/nstream/__init__.py so it can never drift
# from the PKGBUILD's pkgver (the script asserts they match).
#
# Usage:  packaging/build-local.sh [--install]
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"

ver="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/src/nstream/__init__.py")"
[[ -n "$ver" ]] || {
	echo "build-local: cannot read __version__"
	exit 1
}

pkgver="$(sed -n 's/^pkgver=\(.*\)/\1/p' "$here/PKGBUILD")"
if [[ "$ver" != "$pkgver" ]]; then
	echo "build-local: version drift — __init__.py=$ver but PKGBUILD pkgver=$pkgver" >&2
	exit 1
fi

echo "==> archiving HEAD as nstream-$ver.tar.gz"
git -C "$root" archive HEAD --prefix="nstream-$ver/" -o "$here/nstream-$ver.tar.gz"

cd "$here"
rm -rf src pkg
echo "==> makepkg (build + check)"
makepkg -f --noconfirm
echo "==> regenerating .SRCINFO"
makepkg --printsrcinfo >.SRCINFO

if [[ "${1:-}" == "--install" ]]; then
	echo "==> installing"
	sudo pacman -U --noconfirm "nstream-$ver-1-any.pkg.tar.zst"
fi

echo "==> done: $here/nstream-$ver-1-any.pkg.tar.zst"
