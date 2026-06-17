#!/usr/bin/env python3
"""
ovn_uplink_sync.py — unify DHCP/lease-change hooks from multiple clients
into one generic OVN router-port/SNAT/route update.

Single file, stdlib only. Subcommands act as thin adapters that normalize
whatever environment/arguments a given DHCP client or networkd-dispatcher
gives us into one generic LeaseEvent, then call apply_lease().

Supported entry points:

  dhclient    — invoked from dhclient-script's exit-hooks. Reads dhclient's
                exported env vars ($reason, $interface, $new_ip_address, ...).

  dhcpcd      — invoked from dhcpcd.exit-hook. Reads dhcpcd's exported env
                vars ($reason, $interface, $new_ip_address, $new_subnet_cidr, ...).

  networkd    — invoked from networkd-dispatcher (per-interface hook dir).
                Reads NETWORKD_* env vars set by networkd-dispatcher, then
                shells out to `networkctl status --json <iface>` to get the
                actual lease facts (networkd-dispatcher itself doesn't pass
                lease details in the environment, just the state change).

  apply       — manual/testing entry point: pass the lease fields directly
                as CLI flags, skipping any client-specific parsing.

Mapping table (interface -> OVN router/port/mac) is intentionally external,
in /etc/ovn-uplink-sync/mapping.json, so adding a new uplink doesn't require
touching this script.

Example mapping.json:
{
  "dhcp-probe-1280": {
    "router": "router-uplink",
    "port": "lrp-uplink-1280-dhcp",
    "mac": "00:00:c0:a8:84:51",
    "backbone_subnet": "10.80.0.0/16"
  }
}
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAPPING_PATH = Path("/etc/ovn-uplink-sync/mapping.json")
LOG = logging.getLogger("ovn-uplink-sync")

# reasons across clients that mean "we have a usable, current lease"
BOUND_REASONS = {
    "BOUND", "RENEW", "REBIND", "REBOOT",          # dhclient
    "BOUND4", "RENEW4", "REBIND4",                  # dhcpcd (v4)
    "configured", "carrier",                        # networkd-dispatcher states
}
# reasons that mean "lease gone, do NOT touch existing OVN config blindly"
RELEASE_REASONS = {
    "EXPIRE", "FAIL", "RELEASE", "STOP",             # dhclient
    "EXPIRE4", "FAIL4", "RELEASE4", "NAK",           # dhcpcd
    "off", "no-carrier", "degraded",                 # networkd-dispatcher states
}


@dataclass
class LeaseEvent:
    interface: str
    state: str               # "bound" | "released" | "ignored"
    ip: Optional[str] = None         # dotted, e.g. "192.168.132.107"
    prefix: Optional[int] = None     # e.g. 24
    gateway: Optional[str] = None    # e.g. "192.168.132.1"
    source: str = "unknown"          # "dhclient" | "dhcpcd" | "networkd" | "manual"


# ───────────────────────── generic core ─────────────────────────

def load_mapping() -> dict:
    if not MAPPING_PATH.exists():
        LOG.error("mapping file not found: %s", MAPPING_PATH)
        return {}
    try:
        return json.loads(MAPPING_PATH.read_text())
    except json.JSONDecodeError as e:
        LOG.error("mapping file invalid JSON: %s", e)
        return {}


def nbctl(*args: str, check: bool = True) -> str:
    cmd = ["ovn-nbctl", *args]
    LOG.debug("exec: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        LOG.warning("ovn-nbctl failed (%s): %s", " ".join(cmd), result.stderr.strip())
    return result.stdout.strip()


def current_port_ip(port: str) -> Optional[str]:
    out = nbctl("--if-exists", "get", "logical_router_port", port, "networks", check=False)
    if not out or out == "[]":
        return None
    # out looks like: ["192.168.132.107/24"]
    out = out.strip("[]").strip('"')
    return out.split("/")[0] if out else None


def apply_lease(event: LeaseEvent, mapping: dict) -> None:
    cfg = mapping.get(event.interface)
    if cfg is None:
        LOG.info("no OVN mapping for interface=%s, ignoring (source=%s)",
                  event.interface, event.source)
        return

    router = cfg["router"]
    port = cfg["port"]
    mac = cfg["mac"]
    backbone_subnet = cfg.get("backbone_subnet", "10.80.0.0/16")

    if event.state == "released":
        LOG.info("interface=%s lease released (source=%s) — leaving last-known "
                  "OVN config on %s/%s in place", event.interface, event.source,
                  router, port)
        return

    if event.state != "bound" or not event.ip:
        LOG.info("interface=%s state=%s has no usable lease, ignoring",
                  event.interface, event.state)
        return

    prefix = event.prefix if event.prefix is not None else 24
    new_cidr = f"{event.ip}/{prefix}"

    existing_ip = current_port_ip(port)
    if existing_ip == event.ip:
        LOG.info("interface=%s: %s already at %s, no change", event.interface, port, event.ip)
        return

    LOG.info("interface=%s (%s): updating %s -> %s (was %s)",
              event.interface, event.source, port, new_cidr, existing_ip)

    if existing_ip is None:
        nbctl("--may-exist", "lrp-add", router, port, mac, new_cidr)
    else:
        nbctl("set", "logical_router_port", port, f'networks=["{new_cidr}"]')

    nbctl("--if-exists", "lr-nat-del", router, "snat", backbone_subnet, check=False)
    nbctl("lr-nat-add", router, "snat", event.ip, backbone_subnet)
    LOG.info("SNAT %s -> %s applied on %s", backbone_subnet, event.ip, router)

    if event.gateway:
        nbctl("--if-exists", "lr-route-del", router, "0.0.0.0/0", check=False)
        nbctl("lr-route-add", router, "0.0.0.0/0", event.gateway)
        LOG.info("default route on %s -> %s applied", router, event.gateway)


# ───────────────────────── adapters ─────────────────────────

def mask_to_prefix(mask: str) -> int:
    return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen


def adapter_dhclient() -> LeaseEvent:
    """dhclient-script exports these as env vars before sourcing exit-hooks."""
    reason = os.environ.get("reason", "")
    interface = os.environ.get("interface", "")
    ip = os.environ.get("new_ip_address")
    mask = os.environ.get("new_subnet_mask")
    routers = os.environ.get("new_routers", "")

    if reason in BOUND_REASONS and ip:
        prefix = mask_to_prefix(mask) if mask else 24
        gw = routers.split()[0] if routers else None
        return LeaseEvent(interface, "bound", ip, prefix, gw, source="dhclient")
    if reason in RELEASE_REASONS:
        return LeaseEvent(interface, "released", source="dhclient")
    return LeaseEvent(interface, "ignored", source="dhclient")


def adapter_dhcpcd() -> LeaseEvent:
    """dhcpcd exit-hook exports these. new_subnet_cidr is already an int."""
    reason = os.environ.get("reason", "")
    interface = os.environ.get("interface", "")
    ip = os.environ.get("new_ip_address")
    cidr = os.environ.get("new_subnet_cidr")
    routers = os.environ.get("new_routers", "")

    if reason in BOUND_REASONS and ip:
        prefix = int(cidr) if cidr else 24
        gw = routers.split()[0] if routers else None
        return LeaseEvent(interface, "bound", ip, prefix, gw, source="dhcpcd")
    if reason in RELEASE_REASONS:
        return LeaseEvent(interface, "released", source="dhcpcd")
    return LeaseEvent(interface, "ignored", source="dhcpcd")


def adapter_networkd(interface: str) -> LeaseEvent:
    """
    networkd-dispatcher passes interface name + new state via env vars
    ($IFACE, $STATE) but no lease facts — query networkctl for those.
    """
    state = os.environ.get("STATE", "")
    iface = interface or os.environ.get("IFACE", "")

    if state in RELEASE_REASONS:
        return LeaseEvent(iface, "released", source="networkd")
    if state not in BOUND_REASONS:
        return LeaseEvent(iface, "ignored", source="networkd")

    try:
        out = subprocess.run(
            ["networkctl", "status", "--json=short", iface],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        LOG.warning("networkctl query failed for %s: %s", iface, e)
        return LeaseEvent(iface, "ignored", source="networkd")

    addrs = data.get("Addresses", []) or data.get("AddressFamilies", [])
    ip, prefix = None, None
    for a in addrs:
        fam = a.get("Family")
        if fam == 2:  # AF_INET
            ip = a.get("Address")
            prefix = a.get("PrefixLength", 24)
            break

    gw = None
    for r in data.get("Routes", []):
        if r.get("Destination") in (None, "0.0.0.0/0", ""):
            gw = r.get("Gateway")
            break

    if not ip:
        LOG.info("networkd: no IPv4 address found for %s yet", iface)
        return LeaseEvent(iface, "ignored", source="networkd")

    return LeaseEvent(iface, "bound", ip, prefix, gw, source="networkd")


# ───────────────────────── CLI ─────────────────────────

def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("OVN_UPLINK_SYNC_DEBUG") else logging.INFO,
        format="[ovn-uplink-sync] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} {{dhclient|dhcpcd|networkd|apply}} [args]", file=sys.stderr)
        return 2

    mode = sys.argv[1]
    mapping = load_mapping()

    if mode == "dhclient":
        event = adapter_dhclient()
    elif mode == "dhcpcd":
        event = adapter_dhcpcd()
    elif mode == "networkd":
        iface = sys.argv[2] if len(sys.argv) > 2 else ""
        event = adapter_networkd(iface)
    elif mode == "apply":
        # manual: ovn_uplink_sync.py apply <iface> <ip> <prefix> [gateway]
        if len(sys.argv) < 5:
            print("usage: apply <iface> <ip> <prefix> [gateway]", file=sys.stderr)
            return 2
        iface, ip, prefix = sys.argv[2], sys.argv[3], int(sys.argv[4])
        gw = sys.argv[5] if len(sys.argv) > 5 else None
        event = LeaseEvent(iface, "bound", ip, prefix, gw, source="manual")
    else:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2

    apply_lease(event, mapping)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
