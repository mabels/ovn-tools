# uplink-sync

Keeps an OVN logical router port — plus its SNAT rule and default route —
in sync with a dynamically-leased DHCPv4 address on a real interface.

## Why

OVN logical router ports take a *static* IP/subnet. There's no built-in way
for one to track a dynamically-leased WAN address (cable modem, DSL,
LTE/Starlink uplink, etc.) the way a normal Linux interface does via
`dhclient`/`dhcpcd`/`systemd-networkd`.

## How it works

The real DHCP client (`dhclient`, currently) runs directly on the uplink's
interface — which is also an OVS bridge member feeding an OVN-mapped
provider bridge. The DHCP client doesn't need to know anything about OVN;
it behaves exactly as it always would. On every lease event
(BOUND/RENEW/EXPIRE/...), its exit-hook calls back into
`ovn_uplink_sync.py`, which:

- updates the configured OVN logical router port's `networks` to match
  the current lease IP/prefix (`ovn-nbctl set logical_router_port ...`)
- replaces the SNAT external address so internal traffic keeps
  translating correctly when the lease changes (`ovn-nbctl lr-nat-add/del
  ... snat ...`)
- replaces the default route to point at the lease's gateway
  (`ovn-nbctl lr-route-add/del ... 0.0.0.0/0 ...`)

Updates are skipped entirely if the lease's IP hasn't actually changed, so
routine renewals are no-ops rather than flapping NAT/conntrack state.

The script owns its own lifecycle: it installs its own dhclient hook file
(idempotently, safe to call repeatedly) and is itself what systemd
supervises — `run <uplink>` execs the configured DHCP client in the
foreground, so systemd ends up supervising the real client process
directly (journal capture, `Restart=`, etc. all work normally).

This deliberately does **not** use `systemd-networkd` for the uplink
interface — networkd insists on trying (and failing, loudly but
non-fatally) to set itself as the interface's netlink master alongside
OVS, and in practice this leaves the interface stuck in `enslaved
(configuring)` rather than ever reaching a state where DHCP completes
cleanly. Running `dhclient` directly, supervised by its own systemd unit,
sidesteps that entirely. See the project history / commit log for the
debugging trail if you hit the same issue.

## Files

- `ovn_uplink_sync.py` — the tool. Single file, Python 3 stdlib only.
  Install to `/usr/local/bin/ovn_uplink_sync.py`.
- `uplinks.example.json` — copy to `/etc/ovn-uplink-sync/uplinks.json` and
  edit for your topology.
- `systemd/ovn-uplink-sync@.service` — template unit. Install to
  `/etc/systemd/system/ovn-uplink-sync@.service`.
- `hooks/dhcpcd.exit-hook`,
  `hooks/networkd-dispatcher-50-ovn-uplink-sync.sh` — manual-install hook
  templates for dhcpcd/networkd-dispatcher. The `dhclient` adapter is
  self-installing via `ovn_uplink_sync.py install`; these other two
  clients aren't wired into `install`/`run` yet (see Known limitations),
  so install them by hand if you need them today.

## uplinks.json format

```json
{
  "<name>": {
    "interface": "<real interface name the DHCP client runs against>",
    "router": "<OVN logical router name>",
    "port": "<OVN logical router port name to keep in sync>",
    "mac": "<MAC to assign if the port doesn't exist yet>",
    "backbone_subnet": "<subnet to SNAT toward this uplink, e.g. 10.80.0.0/16>",
    "client": "dhclient"
  }
}
```

`<name>` is also the systemd template instance name — e.g. an entry keyed
`"1280"` is brought up with `systemctl enable --now
ovn-uplink-sync@1280.service`.

## Installing

```sh
cp ovn_uplink_sync.py /usr/local/bin/ovn_uplink_sync.py
chmod +x /usr/local/bin/ovn_uplink_sync.py

mkdir -p /etc/ovn-uplink-sync
cp uplinks.example.json /etc/ovn-uplink-sync/uplinks.json
# edit to match your topology

cp systemd/ovn-uplink-sync@.service /etc/systemd/system/
systemctl daemon-reload
```

## Bringing up an uplink

```sh
systemctl enable --now ovn-uplink-sync@<name>.service
```

That's the entire deployment step for a new uplink once it has an entry in
`uplinks.json` — no separate per-interface unit file to write by hand.
`ExecStartPre` calls `install <name>` (idempotent — installs the dhclient
hook if it isn't already in place), then `ExecStart` calls `run <name>`,
which execs `dhclient` directly so systemd supervises it.

## Manual / testing

```sh
python3 ovn_uplink_sync.py apply <interface> <ip> <prefix> [gateway]
```

Bypasses all client-specific parsing and applies a lease directly — useful
for testing the OVN-side logic without a live DHCP transaction.

## Debugging

```sh
OVN_UPLINK_SYNC_DEBUG=1 python3 ovn_uplink_sync.py apply ...
journalctl -u ovn-uplink-sync@<name>.service -f
```

`OVN_UPLINK_SYNC_DEBUG=1` enables debug-level logging, including every
`ovn-nbctl` invocation.

## Known limitations

- IPv4 only. IPv6/SLAAC support is planned but not implemented — OVN
  router ports have no equivalent "client" concept for SLAAC the way they
  at least conceptually map onto a DHCPv4 lease, so this will likely need
  its own netns-based watcher rather than reusing the DHCP adapter
  pattern.
- Only `dhclient` is wired into `install`/`run` (`CLIENT_COMMANDS`).
  `dhcpcd` and `networkd-dispatcher` have working adapter functions
  (`adapter_dhcpcd`, and the `dhclient`/`dhcpcd` CLI subcommands) but
  aren't yet supervised the same way — install their hook templates from
  `hooks/` by hand and run the client yourself if you need one of those
  today.
- No automated tests yet (would need a fake `ovn-nbctl` on PATH to test
  the apply logic without a live OVN instance).
