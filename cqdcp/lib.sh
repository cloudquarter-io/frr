# SPDX-FileCopyrightText: © 2026 cloudquarter.io
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Shared by build-debs.sh and test-debs.sh: the build inputs (build.env), the
# source tree (this checkout) and the Debian version the build carries.

set -euo pipefail

CQDCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRR_SRC="$(cd "${CQDCP_DIR}/.." && pwd)"
# shellcheck source=cqdcp/build.env
. "${CQDCP_DIR}/build.env"

# The Debian version of the build: frr-stable's revision of the same upstream
# version (`<upstream>-0~ubuntu<release>.1`) plus the suffix. Sorts above the
# stock package and below the next upstream revision.
frr_deb_version() {
  echo "${FRR_UPSTREAM_VERSION}-0~ubuntu${DEB_DISTRO_VERSION}.1${DEB_REVISION_SUFFIX}"
}

# The frr-stable apt source for the build host's Ubuntu release: the build
# dependencies (libyang 3) and the stock package the mutation run installs
# come from it, as on the gateways.
frr_stable_apt_source() {
  local keyring=/usr/share/keyrings/frrouting.gpg
  local list=/etc/apt/sources.list.d/frr-stable.list
  [ -s "$keyring" ] || curl -fsSL https://deb.frrouting.org/frr/keys.gpg -o "$keyring"
  echo "deb [signed-by=${keyring}] https://deb.frrouting.org/frr ${DEB_DISTRIBUTION} frr-stable" >"$list"
  apt-get update -qq
}

# frr_source_check: the checkout is the source. It must carry the upstream
# version build.env records (configure.ac) and the upstream tag must be an
# ancestor of HEAD, so the manifest's list of changes is the commits after it.
frr_source_check() {
  local v
  v="$(sed -n 's/^AC_INIT(\[frr\], \[\([^]]*\)\].*/\1/p' "${FRR_SRC}/configure.ac")"
  if [ "$v" != "$FRR_UPSTREAM_VERSION" ]; then
    echo "frr: configure.ac carries ${v}, build.env records ${FRR_UPSTREAM_VERSION}" >&2
    return 1
  fi
  if ! git -C "$FRR_SRC" merge-base --is-ancestor "$FRR_UPSTREAM_TAG" HEAD 2>/dev/null; then
    echo "frr: ${FRR_UPSTREAM_TAG} is not an ancestor of HEAD (fetch the upstream tags)" >&2
    return 1
  fi
  echo "frr: $(git -C "$FRR_SRC" describe --tags --always) = ${FRR_UPSTREAM_TAG} + $(git -C "$FRR_SRC" rev-list --count "${FRR_UPSTREAM_TAG}..HEAD") commit(s)"
}
