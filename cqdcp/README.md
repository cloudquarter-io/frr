# The cloudquarter build of FRR

This fork carries the FRR release the CQDCP EVPN gateways (VTEPs, DCGWs and
route servers) run, plus the fixes that release lacks. The gateways install it
as Debian packages served by their region's API; nothing else is built here.

FRR is GPL-2.0-or-later. Everything in this directory and every commit after
the upstream release tag is distributed under the same licence; the source of
each published package is the tag it was built from, in this repository.

## Branches and tags

- `cqdcp/<upstream series>` (today `cqdcp/10.7.1`): the upstream release tag
  (`frr-10.7.1`) followed by the fixes, one commit each, with their topotests.
  Upstream branches (`master`, `stable/*`) are mirrored unchanged.
- A release is a tag `frr-<upstream version>-cq.<n>` on that branch. The
  packages it builds carry the Debian version
  `<upstream>-0~ubuntu<release>.1<DEB_REVISION_SUFFIX>` from `build.env`,
  which sorts above frr-stable's stock package of the same version and below
  the next upstream revision; the gateways pin that exact version.
- A tag that has been published is never rebuilt: a changed patch set bumps
  `DEB_REVISION_SUFFIX` in `build.env` and takes the next `-cq.<n>`.

## What the fixes are

FRR 10.7.1 can leave a tenant VRF's EVPN type-5 route advertised after the
path it was exported from is gone, so remote gateways keep forwarding to a
VTEP that no longer holds the prefix (every flow hashed onto it is
black-holed until bgpd restarts). The branch closes three shapes of it:

1. `bgpd: withdraw old local type-5 when the VRF best path moves` — the
   backport of FRRouting/frr#23606: when the best path moves between two
   exportable paths, the type-5 keyed by the old best path is withdrawn.
2. `bgpd: export and withdraw the bulk type-5 routes by the VRF best path` —
   the bulk export every route-map change runs exports only the selected
   path, and the bulk withdrawal removes every local type-5 the VRF
   originated instead of the first entry's alone.
3. The topotests `bgp_evpn_type5_bestpath_change` (upstream),
   `bgp_evpn_type5_bulk_export` and `bgp_evpn_type5_withdraw_sweep`: each
   fails on the stock package and passes on the build.

FRR 10.7.1 can also drop the BFD session of a neighbour that inherits BFD
from a peer-group, while bgpd keeps reporting it Up: when the neighbour is
deleted (a dynamic neighbour whose session drops, or a configured one moved
or re-added), its session is only deregistered once the old peer object is
freed, after its routes are cleared; a new peer object for the same neighbour
registers the same session first, and the late deregistration deletes it.
The route servers' listen-range clients hit this. The branch closes it with:

4. `bgpd: release a deleted peer's BFD session before the peer is freed` —
   the backport of FRRouting/frr#24176 (open upstream): a peer being deleted
   releases its session at once instead of keeping the group's BFD.
5. The topotest `bgp_bfd_peer_group_move` from the same pull request,
   installing its routes with staticd instead of sharpd (the Debian packages
   ship no sharpd): it fails on the stock package and passes on the build.
   The pull request's `bgp_ospf_bfd_peer_group_move` is carried as upstream
   wrote it (it needs sharpd) but not run: in a staticd version of it, the
   first check, OSPF's BFD removed with bgpd left as the session's only
   client, fails on the stock package and on the build alike, before any
   peer-group move, because 10.7.1's bfdd takes a session down whenever one
   of its clients deregisters, even while another still holds it.

## Build, proof and publication

`.github/workflows/cqdcp-packages.yaml` runs on every push to a `cqdcp/**`
branch and on every `frr-*-cq.*` tag, on GitHub-hosted `ubuntu-24.04` runners
(the topotests need root and kernel modules):

1. `cqdcp/build-debs.sh out` builds `frr` and `frr-pythontools` for the
   Ubuntu release in `build.env` through FRR's own `debian/` packaging and
   writes `SHA256SUMS` and `manifest.json` (version, source tag and commit,
   the commits after the upstream tag, each package's file and sha256).
2. `cqdcp/test-debs.sh fail stock` installs frr-stable's stock package of
   the same upstream version and requires every test module to fail;
   `cqdcp/test-debs.sh pass out` installs the build and requires every test
   to pass. A run that never reaches the tests is an error, not a verdict.
3. On a tag, and only after both, the workflow builds `cqdcp/Containerfile`
   (busybox plus `/debs`) and pushes `ghcr.io/cloudquarter-io/frr:<tag>`. The
   repository is public, so the package is public: the API pod needs no
   registry credential to run it. The run's summary prints the digest; a
   tag that already exists in the registry fails the run.

To build locally: a fresh Ubuntu 24.04 host (a VM, not a laptop), then
`sudo cqdcp/build-debs.sh out` and `sudo cqdcp/test-debs.sh pass out`.

## How CQDCP consumes it

The CQDCP chart value `evpn.frr.packagesImage` names the image by digest
(`ghcr.io/cloudquarter-io/frr@sha256:…`). The API runs it as an init
container, serves the packages at `GET /peers/evpn/frr`, and each gateway's
install installs them when its own `frr` differs, pins the exact version and
restarts FRR under the host lock. `deploy/frr/README.md` in the `cqdcp`
repository records the digest each release pinned and the lab-matrix runs
that qualified it.

## Taking a new upstream release

1. Fetch upstream, branch `cqdcp/<new series>` from its release tag, and
   rebase the fix commits onto it; drop each fix the release already carries
   (the commit message names the upstream change).
2. Set `FRR_UPSTREAM_TAG`, `FRR_UPSTREAM_VERSION` and, if the Ubuntu release
   changed, `DEB_DISTRIBUTION`/`DEB_DISTRO_VERSION` in `build.env`; reset
   `DEB_REVISION_SUFFIX` to `+cqdcp1`.
3. Push the branch; the workflow must be green (stock fails, build passes).
   Tag `frr-<version>-cq.1` and push the tag; note the digest it publishes.
4. In `cqdcp`: move `SVD_FRR_SERIES` in both substrates if the series
   changed, qualify the build on the lab matrix (the `type5bulk` round with
   `FRR10_IMAGE` built from the new packages), then pin the digest in each
   region's `evpn.frr.packagesImage` and run the fleet upgrade.

When an upstream release carries every fix, the branch for that series needs
no fix commits: publish it anyway if the gateways should run it pinned, or
set `evpn.frr.packagesImage` empty and let the substrates install
frr-stable's qualified series.
