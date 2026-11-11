#!/usr/bin/env python
# SPDX-License-Identifier: ISC

#
# Copyright (c) 2026 by
# Raul Saura <raul@cloudquarter.io>
#

"""
test_bgp_bfd_zebra_reconnect.py

A BFD session bgpd could not register while it had no connection to zebra is
registered once the connection comes up.

r1's bgpd is restarted with its zebra socket path pointing nowhere and given
its BGP configuration there, so the BFD session its neighbor asks for cannot be
sent ("was not installed"), while r1's bfdd stays registered with zebra. zebra
asks the protocol daemons for a replay of their sessions only when bfdd
(re)connects, so once bgpd reaches zebra nothing but bgpd itself can send the
session again. The neighbor is passive and r2 runs no bgpd: BGP never
connects, so no source, interface or TTL change re-installs the session
either.
"""

import json
import os
import re
import sys

import pytest

CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.common_config import step
from lib.topogen import Topogen, TopoRouter, get_topogen

pytestmark = [pytest.mark.bgpd, pytest.mark.bfdd]

# The zebra socket r1's bgpd is restarted with: absent until the test links
# it to zebra's socket.
LATE_ZAPI = "/var/run/frr/zserv-late.api"
ZAPI = "/var/run/frr/zserv.api"

R1_BGP = """
configure terminal
debug bgp bfd
router bgp 65001
 bgp router-id 192.0.2.1
 no bgp ebgp-requires-policy
 neighbor 192.0.2.2 remote-as 65002
 neighbor 192.0.2.2 passive
 neighbor 192.0.2.2 bfd
"""


def build_topo(tgen):
    tgen.add_router("r1")
    tgen.add_router("r2")

    switch = tgen.add_switch("s1")
    switch.add_link(tgen.gears["r1"])
    switch.add_link(tgen.gears["r2"])


def setup_module(mod):
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    tgen.gears["r1"].load_frr_config(
        os.path.join(CWD, "r1/frr.conf"),
        [
            (TopoRouter.RD_ZEBRA, None),
            (TopoRouter.RD_MGMTD, None),
            (TopoRouter.RD_BFD, None),
            (TopoRouter.RD_BGP, None),
        ],
    )
    tgen.gears["r2"].load_frr_config(
        os.path.join(CWD, "r2/frr.conf"),
        [
            (TopoRouter.RD_ZEBRA, None),
            (TopoRouter.RD_MGMTD, None),
            (TopoRouter.RD_BFD, None),
        ],
    )

    tgen.start_router()


def teardown_module(mod):
    tgen = get_topogen()
    tgen.stop_topology()


def _bfd_peer(router, peer):
    for entry in json.loads(router.vtysh_cmd("show bfd peers json")):
        if entry.get("peer") == peer:
            return entry
    return None


def _bgpd_log():
    tgen = get_topogen()
    with open(os.path.join(tgen.logdir, "r1", "bgpd.log")) as f:
        return f.read()


def _send_failed():
    return re.search(
        r"_bfd_sess_send: BFD session \S+ -> 192\.0\.2\.2 .*was not installed",
        _bgpd_log(),
    ) is not None


def _bgpd_connected():
    out = get_topogen().gears["r1"].vtysh_cmd("show zebra client summary")
    # bgpd's main client reads "bgp"; its label manager client "bgp[0:1]".
    return re.search(r"^bgp\s", out, re.M) is not None


def _check_session_up():
    tgen = get_topogen()
    r1 = tgen.gears["r1"]
    r2 = tgen.gears["r2"]

    entry = _bfd_peer(r1, "192.0.2.2")
    if entry is None:
        return "r1: bfdd has no session for 192.0.2.2"
    if entry.get("status") != "up":
        return "r1: bfdd session 192.0.2.2: {}".format(entry)

    entry = _bfd_peer(r2, "192.0.2.1")
    if entry is None or entry.get("status") != "up":
        return "r2: bfdd session 192.0.2.1: {}".format(entry)

    neigh = json.loads(r1.vtysh_cmd("show bgp neighbors 192.0.2.2 json"))
    bfd = neigh.get("192.0.2.2", {}).get("peerBfdInfo", {})
    if bfd.get("status") != "Up":
        return "r1: bgpd BFD status for 192.0.2.2: {}".format(bfd)

    return None


def test_bfd_send_fails_without_zebra():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    # topotest waits for a daemon it starts with zebra to reach zebra, so
    # bgpd starts connected and is restarted alone without a reachable socket.
    step("Restart r1's bgpd with no reachable zebra socket and configure it")
    r1 = tgen.gears["r1"]
    r1.killDaemons(["bgpd"])
    r1.net.daemons_options["bgpd"] = "-z {}".format(LATE_ZAPI)
    r1.startDaemons(["bgpd"])
    r1.vtysh_cmd(R1_BGP)

    step("r1's bgpd has no zebra connection: its BFD session is not sent")
    _, result = topotest.run_and_expect(_send_failed, True, count=30, wait=1)
    assert result, "r1: bgpd logged no failed BFD send for 192.0.2.2"
    assert not _bgpd_connected(), "r1: bgpd reached zebra before the test allowed it"
    assert (
        _bfd_peer(tgen.gears["r1"], "192.0.2.2") is None
    ), "r1: bfdd holds a session for 192.0.2.2 bgpd could not send"


def test_bfd_sent_once_zebra_is_reached():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("Give r1's bgpd its zebra socket")
    tgen.gears["r1"].cmd_raises("ln -s {} {}".format(ZAPI, LATE_ZAPI))
    _, result = topotest.run_and_expect(_bgpd_connected, True, count=60, wait=1)
    assert result, "r1: bgpd did not connect to zebra"

    step("The session bgpd could not send is registered and comes up")
    _, result = topotest.run_and_expect(_check_session_up, None, count=30, wait=1)
    assert result is None, "after bgpd reached zebra: {}".format(result)


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
