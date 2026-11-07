#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# Copyright (c) 2026 by
# Raul Saura <raul@cloudquarter.io>
#

"""
test_bgp_evpn_type5_withdraw_sweep.py

Check that removing "advertise ipv4 unicast" withdraws every local EVPN
type-5 of the VRF, whichever VRF path exported it.

Local type-5 paths are keyed by the VRF path they were exported from. The
bulk withdrawal withdrew by the key of the first path in the prefix's list
and stopped, so a type-5 exported from another path of the prefix stayed
advertised. A bgpd without the best-path fix of bgp_evpn_type5_bestpath_change
builds exactly that table: when the best path moves between two exportable
paths, the old best path's type-5 is left next to the new one. This test
reproduces that table without asserting on it, and asserts only that the
withdrawal then leaves nothing: a bgpd whose withdrawal walks the VRF path
list is caught here, whether or not it still leaves the old type-5 behind.

Same topology as bgp_evpn_type5_bulk_export: r1 is a VTEP with vrf-a (L3VNI
100) and two eBGP peers in it, r2 (AS 65002) and r3 (AS 65003), announcing
the same /32 under maximum-paths with as-path multipath-relax. r3 starts
shut down so r2's path is the older one and the best.

Topology:

    +----+  s1
    | r1 |------
    +----+
    |    |
 s2 |    | s3
    |    |
 +----+ +----+
 | r2 | | r3 |
 +----+ +----+
"""

import json
import os
import platform
import sys

import pytest

CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.common_config import step
from lib.topogen import Topogen, get_topogen
from lib.topolog import logger

pytestmark = [pytest.mark.bgpd]

VRF = "vrf-a"
PREFIX = "192.0.2.10/32"
EVPN_PREFIX = "[5]:[0]:[32]:[192.0.2.10]"
R2 = "10.0.12.2"
R3 = "10.0.13.3"
R1_FROM_R2 = "10.0.12.1"
R1_FROM_R3 = "10.0.13.1"
PATH_R3 = "65003"


def build_topo(tgen):
    "Build function"

    for rname in ("r1", "r2", "r3"):
        tgen.add_router(rname)

    switch = tgen.add_switch("s1")
    switch.add_link(tgen.gears["r1"])

    switch = tgen.add_switch("s2")
    switch.add_link(tgen.gears["r1"])
    switch.add_link(tgen.gears["r2"])

    switch = tgen.add_switch("s3")
    switch.add_link(tgen.gears["r1"])
    switch.add_link(tgen.gears["r3"])


def setup_module(mod):
    "Sets up the pytest environment"

    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    krel = platform.release()
    if topotest.version_cmp(krel, "4.18") < 0:
        pytest.skip('EVPN type-5 test requires kernel >= 4.18, found "{}"'.format(krel))

    r1 = tgen.gears["r1"]
    for command in [
        "ip link add {} type vrf table 1001".format(VRF),
        "ip link set dev {} up".format(VRF),
        "ip link set dev r1-eth1 master {}".format(VRF),
        "ip link set dev r1-eth2 master {}".format(VRF),
        "ip link add name br100 up type bridge stp_state 0",
        "ip link set dev br100 master {}".format(VRF),
        "ip link set dev br100 up",
        "ip link add name vxlan100 type vxlan id 100 dstport 4789 "
        "dev r1-eth0 local 192.168.100.1 nolearning",
        "ip link set dev vxlan100 master br100",
        "ip link set dev vxlan100 up type bridge_slave "
        "learning off flood off mcast_flood off",
    ]:
        logger.info("r1: %s", command)
        r1.cmd_raises(command)

    for rname, router in tgen.routers().items():
        router.load_frr_config(os.path.join(CWD, "{}/frr.conf".format(rname)))
    tgen.start_router()


def teardown_module(_mod):
    "Teardown the pytest environment"

    tgen = get_topogen()
    tgen.stop_topology()


def _vrf_paths(router):
    "Return the (peer, best) of the VRF paths for PREFIX, sorted by peer."

    output = json.loads(
        router.vtysh_cmd("show bgp vrf {} ipv4 unicast {} json".format(VRF, PREFIX))
        or "{}"
    )
    return sorted(
        (path.get("peer", {}).get("peerId"), bool(path.get("bestpath", {}).get("overall")))
        for path in output.get("paths", [])
    )


def _evpn_type5_paths(router):
    "Return the AS paths of the EVPN type-5 paths for PREFIX, all RDs."

    output = json.loads(
        router.vtysh_cmd("show bgp l2vpn evpn route type prefix json") or "{}"
    )
    paths = []
    for rd_routes in output.values():
        if not isinstance(rd_routes, dict):
            continue
        for path in rd_routes.get(EVPN_PREFIX, {}).get("paths", []):
            paths.extend(entry.get("path") for entry in path)
    return sorted(paths)


def _wait_for(router, check, expected, what):
    _, result = topotest.run_and_expect(check, expected, count=30, wait=1)
    assert result == expected, "{} {}: expected {}, got {}\n{}\n{}".format(
        router.name,
        what,
        expected,
        result,
        router.vtysh_cmd("show bgp vrf {} ipv4 unicast {}".format(VRF, PREFIX)),
        router.vtysh_cmd("show bgp l2vpn evpn route type prefix"),
    )


def _config(rname, commands):
    router = get_topogen().gears[rname]
    output = router.vtysh_cmd("configure terminal\n{}\nend\n".format(commands))
    assert "% Unknown" not in output and "% Invalid" not in output, output


def _set_peer_up(rname, local_as, neighbor, up):
    _config(
        rname,
        "router bgp {}\n{}neighbor {} shutdown".format(
            local_as, "no " if up else "", neighbor
        ),
    )


def _set_advertise(present):
    _config(
        "r1",
        "router bgp 65001 vrf {}\n"
        "address-family l2vpn evpn\n"
        "{}advertise ipv4 unicast".format(VRF, "" if present else "no "),
    )


def test_bgp_evpn_type5_withdraw_sweep():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)
    r1 = tgen.gears["r1"]

    step("r2 up alone, then r3: two paths, r2's older path the best")
    _wait_for(r1, lambda: _vrf_paths(r1), [(R2, True)], "VRF paths")
    _set_peer_up("r3", 65003, R1_FROM_R3, True)
    _wait_for(r1, lambda: _vrf_paths(r1), sorted([(R2, True), (R3, False)]), "VRF paths")
    # The EVPN table is deliberately not asserted here: a bgpd without the
    # best-path fix leaves r2's type-5 next to r3's after the next step.

    step("Shut r2: the best path moves to r3")
    _set_peer_up("r2", 65002, R1_FROM_R2, False)
    _wait_for(r1, lambda: _vrf_paths(r1), [(R3, True)], "VRF paths")
    _wait_for(r1, lambda: PATH_R3 in _evpn_type5_paths(r1), True, "a type-5 from r3")

    step("no advertise ipv4 unicast: no type-5 is left, whichever path exported it")
    _set_advertise(False)
    _wait_for(r1, lambda: _evpn_type5_paths(r1), [], "EVPN type-5")

    step("advertise ipv4 unicast: exactly one type-5, from the best path (r3)")
    _set_advertise(True)
    _wait_for(r1, lambda: _evpn_type5_paths(r1), [PATH_R3], "EVPN type-5")

    step("Shut r3: no path, no type-5")
    _set_peer_up("r3", 65003, R1_FROM_R3, False)
    _wait_for(r1, lambda: _vrf_paths(r1), [], "VRF paths")
    _wait_for(r1, lambda: _evpn_type5_paths(r1), [], "EVPN type-5")


def test_memory_leak():
    "Run the memory leak test and report results."

    tgen = get_topogen()
    if not tgen.is_memleak_enabled():
        pytest.skip("Memory leak test/report is disabled")

    tgen.report_memory_leaks()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
