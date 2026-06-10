#!/usr/bin/env bash
# Build the Arch package for the current release tag.
#
# The PKGBUILD `source` is a git clone over SSH pinned to `v<pkgver>` (the GitHub
# repo is private, so the anonymous tarball URL would 404): makepkg fetches by
# itself as long as the tag is pushed and the builder's SSH key is loaded.
# This wrapper only adds the guard rails: it asserts pkgver matches
# src/nstream/__init__.py (so the two can never drift), checks the tag is on
# origin, and regenerates .SRCINFO.
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
pkgrel="$(sed -n 's/^pkgrel=\(.*\)/\1/p' "$here/PKGBUILD")"
if [[ "$ver" != "$pkgver" ]]; then
	echo "build-local: version drift — __init__.py=$ver but PKGBUILD pkgver=$pkgver" >&2
	exit 1
fi

if ! git -C "$root" ls-remote --tags origin "v$ver" | grep -q .; then
	echo "build-local: tag v$ver not on origin — push it first (the PKGBUILD clones it)" >&2
	exit 1
fi

cd "$here"
rm -rf src pkg
echo "==> makepkg (clone v$ver over SSH + build + check)"
makepkg -f --noconfirm
echo "==> regenerating .SRCINFO"
makepkg --printsrcinfo >.SRCINFO

if [[ "${1:-}" == "--install" ]]; then
	echo "==> installing"
	sudo pacman -U --noconfirm "nstream-$ver-$pkgrel-any.pkg.tar.zst"
fi

echo "==> done: $here/nstream-$ver-$pkgrel-any.pkg.tar.zst"
