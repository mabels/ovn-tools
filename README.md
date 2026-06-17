# ovn-tools

Operational tooling for [OVN](https://www.ovn.org/) (Open Virtual Network)
that doesn't belong in OVN core but is generically useful for anyone running
OVN outside of a cloud-orchestrator (OpenStack/Kubernetes) context — e.g. a
home/homelab SDN setup where OVN is the router for one or more WAN uplinks.

## Status

Early, actively developed against a real single-chassis OVN deployment.
Tested manually on Ubuntu 26.04 with OVN 26.03 / OVS 3.7. Not yet hardened
for general consumption — config paths, mapping schema, and CLI surface may
still change. Issues and PRs welcome regardless.

## Tools

### uplink-sync — DHCP/lease → OVN router port sync

**Problem:** OVN logical router ports take a *static* IP/subnet. There's no
built-in way for an OVN router port to track a dynamically-leased WAN
address (cable modem, DSL, LTE/Starlink uplink, etc.) the way a normal
Linux interface would via `dhclient`/`dhcpcd`/`systemd-networkd`.

**Approach:** run the actual DHCP client in a network namespace, on an OVS
internal port that's a member of the same OVN-mapped bridge as the real
uplink. The DHCP client sees a completely normal Linux network stack and
behaves exactly as it always does — no OVN-awareness needed on its part.
Its lease-change hook (or networkd-dispatcher, for systemd-networkd-managed
interfaces) calls into `ovn_uplink_sync.py`, which normalizes whichever
client called it into one generic `LeaseEvent` and applies it to OVN:

- `ovn-nbctl set logical_router_port ... networks=[...]` — update the
  port's IP/prefix to match the current lease
- `ovn-nbctl lr-nat-add/del ... snat ...` — keep the SNAT external address
  in sync with the lease, so internal traffic continues to be translated
  correctly when the lease changes
- `ovn-nbctl lr-route-add/del ... 0.0.0.0/0 ...` — keep the default route
  pointed at the current lease's gateway

Updates are only applied when the lease's IP actually changed (checked
against current OVN state first), so renewals that don't change anything
are no-ops rather than flapping NAT/conntrack state.

See [`uplink-sync/README.md`](./uplink-sync/README.md) for installation
and the mapping file format.

## License

MIT — see [LICENSE](./LICENSE).
