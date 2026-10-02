#!/usr/bin/env bash
# Build the Arch package for the current release tag.
#
# The PKGBUILD `source` is the GitHub source tarball for `v<pkgver>`. This wrapper
# asserts pkgver matches src/nstream/__init__.py, checks the tag is on origin,
# and regenerates .SRCINFO. After a public tag, replace sha256sums=('SKIP') with
# the tarball hash before uploading to AUR.
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
	echo "build-local: tag v$ver not on origin — push it first (the PKGBUILD fetches the GitHub tarball)" >&2
	exit 1
fi

cd "$here"
# Drop leftover makepkg trees and stale packages/sigs so the tree stays small
# (all of these are gitignored; only PKGBUILD + helpers are tracked).
rm -rf src pkg nstream
rm -f ./*.pkg.tar.zst ./*.tar.gz ./*.sig
echo "==> makepkg (GitHub tarball v$ver + build + check)"
makepkg -f --noconfirm
echo "==> regenerating .SRCINFO"
makepkg --printsrcinfo >.SRCINFO

if [[ "${1:-}" == "--install" ]]; then
	echo "==> installing"
	sudo pacman -U --noconfirm "nstream-$ver-$pkgrel-any.pkg.tar.zst"
fi

echo "==> done: $here/nstream-$ver-$pkgrel-any.pkg.tar.zst"
echo "    (optional cleanup: rm -rf src pkg nstream && rm -f ./*.sig)"
