#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 cloudquarter.io
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Builds the frr and frr-pythontools packages the CQDCP gateways run from this
# checkout, through FRR's own debian/ packaging, on the Ubuntu release the
# gateways run. Runs as root on a fresh host of that release (the
# cqdcp-packages workflow); it installs build dependencies and writes an apt
# source.
#
#   cqdcp/build-debs.sh [out-dir]     default ./out
#
# Output: the two .deb files, SHA256SUMS over them, and manifest.json (the
# version, the source tag and commit, the distribution, the commits after the
# upstream release and each package's file and sha256) — the listing the
# CQDCP API serves its gateways from (GET /peers/evpn/frr).

set -euo pipefail
# shellcheck source=cqdcp/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

out="$(mkdir -p "${1:-out}" && cd "${1:-out}" && pwd)"
version="$(frr_deb_version)"

[ "$(id -u)" = 0 ] || { echo "build-debs.sh: run as root (it installs packages)" >&2; exit 2; }
[ "$(lsb_release -sc)" = "$DEB_DISTRIBUTION" ] || { echo "build-debs.sh: build on ${DEB_DISTRIBUTION}, this host is $(lsb_release -sc)" >&2; exit 2; }

export DEBIAN_FRONTEND=noninteractive
frr_stable_apt_source
apt-get install -y -qq --no-install-recommends git devscripts equivs build-essential fakeroot lsb-release jq ca-certificates curl

frr_source_check
src_tag="$(git -C "$FRR_SRC" describe --tags --exact-match 2>/dev/null || git -C "$FRR_SRC" rev-parse --short HEAD)"
src_commit="$(git -C "$FRR_SRC" rev-parse HEAD)"

# dpkg-buildpackage writes the packages next to the source tree; build in a
# copy so the checkout stays clean (the workflow checks nothing in after it).
work="${WORKDIR:-$(mktemp -d)}"
git -C "$FRR_SRC" worktree add -q --detach "${work}/frr" HEAD
trap 'git -C "$FRR_SRC" worktree remove --force "${work}/frr" 2>/dev/null || true' EXIT
cd "${work}/frr"
mk-build-deps -i -r -t 'apt-get -y -qq --no-install-recommends' debian/control

# The changelog entry names the build. --force-distribution: FRR's changelog
# targets Debian unstable; the gateways run Ubuntu.
DEBEMAIL=cqdcp@cloudquarter.io DEBFULLNAME=cqdcp dch --newversion "$version" \
  --distribution "$DEB_DISTRIBUTION" --force-distribution --force-bad-version \
  "cloudquarter build of ${FRR_UPSTREAM_TAG} (${src_tag}): the fixes listed in cqdcp/README.md."

dpkg-buildpackage -us -uc -b -j"$(nproc)"

cd "$out"
rm -f ./*.deb SHA256SUMS manifest.json
cp "${work}/frr_${version}_amd64.deb" "${work}/frr-pythontools_${version}_all.deb" .
sha256sum ./*.deb | sed 's|\./||' >SHA256SUMS

changes_json="$(git -C "$FRR_SRC" log --reverse --format='%H%x09%s' "${FRR_UPSTREAM_TAG}..HEAD" \
  | jq -Rsc 'split("\n") | map(select(length > 0) | split("\t") | {commit: .[0], subject: .[1]})')"
packages_json="$(for f in ./*.deb; do
  f="${f#./}"
  jq -nc --arg name "$(dpkg-deb -f "$f" Package)" --arg file "$f" --arg arch "$(dpkg-deb -f "$f" Architecture)" \
    --arg sha "$(sha256sum "$f" | cut -d' ' -f1)" '{name: $name, file: $file, architecture: $arch, sha256: $sha}'
done | jq -sc .)"
jq -n --arg version "$version" --arg tag "$src_tag" --arg commit "$src_commit" --arg upstream "$FRR_UPSTREAM_TAG" \
  --arg distribution "$DEB_DISTRIBUTION" --argjson changes "$changes_json" --argjson packages "$packages_json" \
  '{version: $version, source: {tag: $tag, commit: $commit, upstream: $upstream, repository: "cloudquarter-io/frr"}, distribution: $distribution, changes: $changes, packages: $packages}' >manifest.json

echo "built ${version} from ${src_tag}:"; cat SHA256SUMS
