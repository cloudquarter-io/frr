#!/usr/bin/env bash
# SPDX-FileCopyrightText: © 2026 cloudquarter.io
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Runs the FRR topotests this branch adds against an installed FRR, and judges
# the run against the expected outcome:
#
#   cqdcp/test-debs.sh pass <deb-dir>    install the built packages; every test passes
#   cqdcp/test-debs.sh fail stock        install frr-stable's stock package of the
#                                        same upstream version; every test module
#                                        fails (the mutation run: the tests have teeth)
#
# Runs as root on a fresh host of the Ubuntu release in build.env (the
# cqdcp-packages workflow): topotests build network namespaces and need the
# vrf, vxlan and dummy kernel modules. This checkout supplies the tests and the
# topotest library; the daemons come from the installed package.

set -euo pipefail
# shellcheck source=cqdcp/lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

expect="${1:?pass|fail}"; what="${2:?<deb-dir>|stock}"
work="${WORKDIR:-$(mktemp -d)}"
tests=(bgp_evpn_type5_bestpath_change bgp_evpn_type5_bulk_export bgp_evpn_type5_withdraw_sweep)

[ "$(id -u)" = 0 ] || { echo "test-debs.sh: run as root" >&2; exit 2; }
case "$expect" in pass|fail) : ;; *) echo "test-debs.sh: expected outcome is pass or fail, not '${expect}'" >&2; exit 2 ;; esac

export DEBIAN_FRONTEND=noninteractive
frr_stable_apt_source
# What the topotest environment check (lib/topogen.py diagnose_env_linux) and the
# developer guide list: gdb is required, not optional.
apt-get install -y -qq --no-install-recommends git iproute2 net-tools iputils-ping iptables gdb tshark python3-pip python3-venv lsb-release ca-certificates curl

if [ "$what" = stock ]; then
  # The stock package of the same upstream version from frr-stable, whatever
  # revision it carries there.
  apt-get install -y -qq --allow-downgrades "frr=${FRR_UPSTREAM_VERSION}-*" "frr-pythontools=${FRR_UPSTREAM_VERSION}-*"
else
  debs="$(cd "$what" && pwd)"
  (cd "$debs" && sha256sum -c SHA256SUMS)
  apt-get install -y -qq --allow-downgrades "${debs}"/frr_*.deb "${debs}"/frr-pythontools_*.deb
fi
installed="$(dpkg-query -W -f='${Version}' frr)"
echo "frr installed: ${installed}"
systemctl stop frr 2>/dev/null || true
systemctl disable frr 2>/dev/null || true

frr_source_check

# The topotest requirements the FRR developer guide lists (doc/developer/topotests.rst).
python3 -m venv --system-site-packages "${work}/venv"
"${work}/venv/bin/pip" install -q wheel
"${work}/venv/bin/pip" install -q 'pytest>=8.3.2' 'pytest-asyncio>=0.24.0' 'pytest-xdist>=3.6.1' 'scapy>=2.4.5' pyyaml xmltodict
modprobe vrf; modprobe vxlan; modprobe dummy; modprobe bridge
sysctl -q -w net.ipv4.ip_forward=1

cd "${FRR_SRC}/tests/topotests"
rc=0
for t in "${tests[@]}"; do
  echo "::group::topotest ${t} on frr ${installed} (expect ${expect})"
  out="${work}/${t}.out"
  "${work}/venv/bin/pytest" -s -rA "$t" 2>&1 | tee "$out" || true
  echo "::endgroup::"
  # The verdict is read from pytest's own summary, never from its exit status
  # alone: a run that never reached the tests (the environment check refused,
  # a collection error) proves nothing either way and is an error of this
  # script, on both the stock and the built packages.
  if grep -q 'environment has errors' "$out"; then
    echo "test-debs.sh: the topotest environment check refused; its log:" >&2
    tail -n 40 /tmp/topotests/exec.log 2>/dev/null >&2 || true
    exit 2
  fi
  summary="$(grep -E '^=+ .*(passed|failed|error).* in [0-9.]+s' "$out" | tail -1)"
  [ -n "$summary" ] || { echo "test-debs.sh: ${t} printed no pytest summary — the tests did not run" >&2; exit 2; }
  if grep -qE '[0-9]+ error' <<<"$summary"; then
    echo "test-debs.sh: ${t} had errors, not test failures: ${summary}" >&2; exit 2
  fi
  if grep -qE '[0-9]+ failed' <<<"$summary"; then outcome=fail; else outcome=pass; fi
  echo "topotest ${t}: ${outcome} (expected ${expect}) — ${summary}"
  [ "$outcome" = "$expect" ] || rc=1
done
if [ "$rc" != 0 ]; then
  echo "test-debs.sh: at least one module did not ${expect} on frr ${installed}" >&2
fi
exit "$rc"
