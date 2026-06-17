# uplink-sync

## Files

- `ovn_uplink_sync.py` — the actual tool. Single file, Python 3 stdlib
  only. Install to `/usr/local/bin/ovn_uplink_sync.py`.
- `mapping.example.json` — copy to `/etc/ovn-uplink-sync/mapping.json` and
  edit for your topology.
- `hooks/` — thin per-client shims. Each one's filename documents where it
  needs to be installed; install only the ones for the DHCP client(s) you
  actually use.

## Prerequisites

This tool assumes you already have an OVN bridge dedicated to the dynamic
uplink (same one-bridge-per-segment pattern used for any OVN provider
network), and that you've created an OVS *internal* port inside that bridge
for the DHCP client to run against, isolated in its own network namespace
so it can't collide with anything else on the host's default namespace:

```sh
ovs-vsctl add-port br-uplink-dyn dhcp-probe-X \
  -- set interface dhcp-probe-X type=internal

ip netns add ns-dhcp-X
ip link set dhcp-probe-X netns ns-dhcp-X
ip netns exec ns-dhcp-X ip link set lo up
ip netns exec ns-dhcp-X ip link set dhcp-probe-X up
```

`ovn-nbctl` talks to the OVSDB northbound socket over a Unix domain socket
path, which is visible from inside the network namespace (network
namespaces don't isolate the filesystem/mount namespace), so the hook
script can call `ovn-nbctl` directly from inside `ns-dhcp-X` with no extra
plumbing.

## mapping.json format

```json
{
  "<dhcp-client-interface-name>": {
    "router": "<OVN logical router name>",
    "port": "<OVN logical router port name to keep in sync>",
    "mac": "<MAC to assign if the port doesn't exist yet>",
    "backbone_subnet": "<subnet to SNAT toward this uplink, e.g. 10.80.0.0/16>"
  }
}
```

One entry per dynamic uplink. The key must match the interface name the
DHCP client sees (e.g. `dhcp-probe-1280`), not the underlying VLAN/physical
interface name.

## Installing the dhclient hook

```sh
mkdir -p /etc/dhcp/dhclient-exit-hooks.d
cp hooks/dhclient-exit-hooks.d-ovn-uplink-sync \
   /etc/dhcp/dhclient-exit-hooks.d/ovn-uplink-sync
chmod +x /etc/dhcp/dhclient-exit-hooks.d/ovn-uplink-sync
```

Then run dhclient inside the namespace against the probe interface:

```sh
ip netns exec ns-dhcp-X dhclient -v dhcp-probe-X
```

## Installing the dhcpcd hook

```sh
cp hooks/dhcpcd.exit-hook /etc/dhcpcd.exit-hook
chmod +x /etc/dhcpcd.exit-hook
```

## Installing the networkd-dispatcher hook

Requires [`networkd-dispatcher`](https://github.com/wertarbyte/networkd-dispatcher)
installed separately.

```sh
cp hooks/networkd-dispatcher-50-ovn-uplink-sync.sh \
   /etc/networkd-dispatcher/configured.d/50-ovn-uplink-sync
chmod +x /etc/networkd-dispatcher/configured.d/50-ovn-uplink-sync
```

Copy/symlink into `routable.d/`, `off.d/`, etc. as needed for the state
transitions you want to react to.

## Manual / testing

```sh
python3 ovn_uplink_sync.py apply <interface> <ip> <prefix> [gateway]
```

Bypasses all client-specific parsing and applies a lease directly — useful
for testing the OVN-side logic without a live DHCP transaction.

## Debugging

```sh
OVN_UPLINK_SYNC_DEBUG=1 python3 ovn_uplink_sync.py apply ...
```

Enables debug-level logging, including every `ovn-nbctl` invocation.

## Known limitations

- IPv4 only. IPv6/SLAAC support is planned but not implemented — OVN router
  ports have no equivalent "client" concept for SLAAC the way they at least
  conceptually map onto a DHCPv4 lease, so this will likely need its own
  netns-based watcher rather than reusing the DHCP adapter pattern.
- The `networkd` adapter's JSON parsing is based on documented
  `networkctl status --json` output but has not yet been exercised against
  a real lease in this repo's testing — treat it as less proven than the
  `dhclient` and `dhcpcd` adapters.
- No automated tests yet (would need a fake `ovn-nbctl` on PATH to test the
  apply logic without a live OVN instance).
