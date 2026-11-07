#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# Copyright (c) 2026 by
# Raul Saura <raul@cloudquarter.io>
#

"""
test_bgp_evpn_type5_bulk_export.py

Check that the bulk EVPN type-5 export and withdrawal act on the VRF best
path, as the per-prefix injection does.

With "advertise ipv4 unicast" (no gateway-ip), a VRF prefix is exported to
EVPN from its best path only, and the local type-5 is keyed by the VRF path
it was exported from. The bulk export that runs when the advertisement is
(re)configured walked the prefix's path list and exported the first path
flagged selected OR multipath, and the bulk withdrawal withdrew the type-5
keyed by that same first path. With two multipath-equal paths for one
prefix, the newer, non-best path is listed first, so:

- "no advertise ipv4 unicast" withdrew by the non-best path's key, found
  nothing, and left the best path's type-5 advertised;
- "advertise ipv4 unicast" exported the non-best path, and when that path
  went away its type-5 stayed, as the per-prefix withdrawal only ever
  withdraws by the best path's key.

Both left a type-5 nothing could withdraw any more, except a bgpd restart.

r1 is a VTEP with vrf-a (L3VNI 100) and two eBGP peers in it, r2 (AS 65002)
and r3 (AS 65003), both announcing 192.0.2.10/32 under maximum-paths with
"bgp bestpath as-path multipath-relax", so the two paths are multipath and
the AS path of the EVPN type-5 names the VRF path it was exported from. r3
starts shut down so r2's path is the older one and the best; r3's path is
the newer one, the non-best multipath entry listed first.

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
PATH_R2 = "65002"
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
    "Return (peer, best, multipath) of the VRF paths for PREFIX, in list order."

    output = json.loads(
        router.vtysh_cmd("show bgp vrf {} ipv4 unicast {} json".format(VRF, PREFIX))
        or "{}"
    )
    return [
        (
            path.get("peer", {}).get("peerId"),
            bool(path.get("bestpath", {}).get("overall")),
            bool(path.get("multipath")),
        )
        for path in output.get("paths", [])
    ]


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


def _check(vrf_paths, evpn_paths):
    r1 = get_topogen().gears["r1"]

    _wait_for(r1, lambda: _vrf_paths(r1), vrf_paths, "VRF paths")
    _wait_for(r1, lambda: _evpn_type5_paths(r1), evpn_paths, "EVPN type-5")


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


# r2 up alone: its path is the best and the only one.
ONLY_R2 = [(R2, True, False)]
# r3 up after r2: r2's older path stays best, r3's newer path is the non-best
# multipath entry and bgpd lists it first. That order is what the bulk export
# and withdrawal walked.
R3_THEN_R2 = [(R3, False, True), (R2, True, True)]


def test_bgp_evpn_type5_initial():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("r2's path is the only one, EVPN has one type-5 exported from it")
    _check(ONLY_R2, [PATH_R2])


def test_bgp_evpn_type5_second_path_keeps_best():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("Bring r3 up: a second, non-best multipath entry, listed first")
    _set_peer_up("r3", 65003, R1_FROM_R3, True)
    _check(R3_THEN_R2, [PATH_R2])


def test_bgp_evpn_type5_bulk_withdraw():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("no advertise ipv4 unicast: the VRF's type-5 is gone")
    _set_advertise(False)
    _check(R3_THEN_R2, [])


def test_bgp_evpn_type5_bulk_export():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("advertise ipv4 unicast: one type-5, exported from the best path")
    _set_advertise(True)
    _check(R3_THEN_R2, [PATH_R2])


def test_bgp_evpn_type5_non_best_path_removed():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("Shut r3: the best path does not change, still one type-5")
    _set_peer_up("r3", 65003, R1_FROM_R3, False)
    _check(ONLY_R2, [PATH_R2])


def test_bgp_evpn_type5_last_path_removed():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("Shut r2: the VRF has no path left and EVPN no type-5")
    _set_peer_up("r2", 65002, R1_FROM_R2, False)
    _check([], [])


def test_bgp_evpn_type5_best_path_removed_first():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("Bring r2 then r3 back: the same two paths, one type-5")
    _set_peer_up("r2", 65002, R1_FROM_R2, True)
    _check(ONLY_R2, [PATH_R2])
    _set_peer_up("r3", 65003, R1_FROM_R3, True)
    _check(R3_THEN_R2, [PATH_R2])

    step("Shut r2: r3's path becomes best, the type-5 follows it")
    _set_peer_up("r2", 65002, R1_FROM_R2, False)
    _check([(R3, True, False)], [PATH_R3])

    step("Shut r3: nothing left")
    _set_peer_up("r3", 65003, R1_FROM_R3, False)
    _check([], [])


def test_memory_leak():
    "Run the memory leak test and report results."

    tgen = get_topogen()
    if not tgen.is_memleak_enabled():
        pytest.skip("Memory leak test/report is disabled")

    tgen.report_memory_leaks()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
