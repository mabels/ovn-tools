#!/usr/bin/env python3
"""
ovn_uplink_sync.py — own the full lifecycle of a dynamic OVN uplink:
supervise the DHCP client, install its hook, and keep the corresponding
OVN logical router port / SNAT / default route in sync with the lease.

Single file, stdlib only.

Configuration lives in one file, one entry per uplink:

  /etc/ovn-uplink-sync/uplinks.json

  {
    "1280": {
      "interface": "ens18.1280",
      "router": "router-uplink",
      "port": "lrp-uplink-1280-dhcp",
      "mac": "00:00:c0:a8:84:51",
      "backbone_subnet": "10.80.0.0/16",
      "client": "dhclient"
    }
  }

The key ("1280" above) is the uplink name used on the CLI and in the
systemd template unit instance (ovn-uplink-sync@1280.service).

Subcommands:

  install <name>   Idempotently write the dhclient exit-hook file so that
                    BOUND/RENEW/etc. on <name>'s interface calls back into
                    this script. Safe to re-run.

  run <name>        Long-running supervisor: this is what systemd's
                    ExecStart should point at. Installs the hook (if not
                    already present) then execs the configured DHCP
                    client in the foreground on the configured interface,
                    so systemd supervises dhclient's actual process
                    (restarts, journal capture, etc. all "just work").

  dhclient          Adapter: called by the installed dhclient exit-hook
                    on every BOUND/RENEW/REBIND/REBOOT/EXPIRE/.../STOP.
                    Looks up the uplink entry by interface name (not by
                    the <name> key) since that's all dhclient-script
                    gives us, then applies the lease to OVN.

  dhcpcd            Same idea, for dhcpcd's exit-hook environment.

  apply             Manual/testing: push a lease directly without going
                    through any client adapter.
                    apply <interface> <ip> <prefix> [gateway]

Only dhclient is wired up as a real supervised client for now (matches
what's actually deployed and tested). dhcpcd is supported as an adapter
for sites that already run dhcpcd instead, but "run"/"install" only know
how to supervise dhclient today — add the dhcpcd equivalent in
CLIENT_COMMANDS below when that's actually needed.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path("/etc/ovn-uplink-sync")
UPLINKS_PATH = CONFIG_DIR / "uplinks.json"
DHCLIENT_HOOK_DIR = Path("/etc/dhcp/dhclient-exit-hooks.d")
DHCLIENT_HOOK_PATH = DHCLIENT_HOOK_DIR / "ovn-uplink-sync"
SELF_PATH = "/usr/local/bin/ovn_uplink_sync.py"

LOG = logging.getLogger("ovn-uplink-sync")

BOUND_REASONS = {
    "BOUND", "RENEW", "REBIND", "REBOOT",
    "BOUND4", "RENEW4", "REBIND4",
}
RELEASE_REASONS = {
    "EXPIRE", "FAIL", "RELEASE", "STOP",
    "EXPIRE4", "FAIL4", "RELEASE4", "NAK",
}

# how to supervise each supported client. {name} is replaced with the
# resolved lease/pid file paths derived from the uplink name.
CLIENT_COMMANDS = {
    "dhclient": [
        "/usr/sbin/dhclient", "-d", "-4",
        "-pf", "/run/dhclient.{name}.pid",
        "-lf", "/var/lib/dhcp/dhclient.{name}.leases",
        "{interface}",
    ],
}


@dataclass
class Uplink:
    name: str
    interface: str
    router: str
    port: str
    mac: str
    backbone_subnet: str
    client: str = "dhclient"


@dataclass
class LeaseEvent:
    interface: str
    state: str               # "bound" | "released" | "ignored"
    ip: Optional[str] = None
    prefix: Optional[int] = None
    gateway: Optional[str] = None
    source: str = "unknown"


# ───────────────────────── config ─────────────────────────

def load_uplinks() -> dict[str, Uplink]:
    if not UPLINKS_PATH.exists():
        LOG.error("uplinks file not found: %s", UPLINKS_PATH)
        return {}
    try:
        raw = json.loads(UPLINKS_PATH.read_text())
    except json.JSONDecodeError as e:
        LOG.error("uplinks file invalid JSON: %s", e)
        return {}

    uplinks = {}
    for name, cfg in raw.items():
        try:
            uplinks[name] = Uplink(
                name=name,
                interface=cfg["interface"],
                router=cfg["router"],
                port=cfg["port"],
                mac=cfg["mac"],
                backbone_subnet=cfg.get("backbone_subnet", "10.80.0.0/16"),
                client=cfg.get("client", "dhclient"),
            )
        except KeyError as e:
            LOG.error("uplink %r missing required field %s, skipping", name, e)
    return uplinks


def find_uplink_by_interface(uplinks: dict[str, Uplink], interface: str) -> Optional[Uplink]:
    for uplink in uplinks.values():
        if uplink.interface == interface:
            return uplink
    return None


# ───────────────────────── OVN core ─────────────────────────

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
    out = out.strip("[]").strip('"')
    return out.split("/")[0] if out else None


def apply_lease(event: LeaseEvent, uplink: Optional[Uplink]) -> None:
    if uplink is None:
        LOG.info("no uplink configured for interface=%s, ignoring (source=%s)",
                  event.interface, event.source)
        return

    if event.state == "released":
        LOG.info("interface=%s lease released (source=%s) — leaving last-known "
                  "OVN config on %s/%s in place", event.interface, event.source,
                  uplink.router, uplink.port)
        return

    if event.state != "bound" or not event.ip:
        LOG.info("interface=%s state=%s has no usable lease, ignoring",
                  event.interface, event.state)
        return

    prefix = event.prefix if event.prefix is not None else 24
    new_cidr = f"{event.ip}/{prefix}"

    existing_ip = current_port_ip(uplink.port)
    if existing_ip == event.ip:
        LOG.info("interface=%s: %s already at %s, no change",
                  event.interface, uplink.port, event.ip)
        return

    LOG.info("interface=%s (%s): updating %s -> %s (was %s)",
              event.interface, event.source, uplink.port, new_cidr, existing_ip)

    if existing_ip is None:
        nbctl("--may-exist", "lrp-add", uplink.router, uplink.port, uplink.mac, new_cidr)
    else:
        nbctl("set", "logical_router_port", uplink.port, f'networks=["{new_cidr}"]')

    nbctl("--if-exists", "lr-nat-del", uplink.router, "snat", uplink.backbone_subnet, check=False)
    nbctl("lr-nat-add", uplink.router, "snat", event.ip, uplink.backbone_subnet)
    LOG.info("SNAT %s -> %s applied on %s", uplink.backbone_subnet, event.ip, uplink.router)

    if event.gateway:
        nbctl("--if-exists", "lr-route-del", uplink.router, "0.0.0.0/0", check=False)
        nbctl("lr-route-add", uplink.router, "0.0.0.0/0", event.gateway)
        LOG.info("default route on %s -> %s applied", uplink.router, event.gateway)


# ───────────────────────── client adapters ─────────────────────────

def mask_to_prefix(mask: str) -> int:
    return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen


def adapter_dhclient() -> LeaseEvent:
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


# ───────────────────────── install / run ─────────────────────────

def install_dhclient_hook() -> None:
    """
    Idempotently install the dash-compatible shim dhclient-script sources
    on every event. dhclient-script uses POSIX '.' to source this file,
    so it must stay plain sh — it just re-execs us under python3.
    """
    DHCLIENT_HOOK_DIR.mkdir(parents=True, exist_ok=True)
    desired = (
        "# Installed by ovn_uplink_sync.py — do not edit by hand.\n"
        "# dhclient-script sources this with dash's '.', so keep it POSIX sh.\n"
        f"python3 {SELF_PATH} dhclient\n"
    )
    if DHCLIENT_HOOK_PATH.exists() and DHCLIENT_HOOK_PATH.read_text() == desired:
        LOG.debug("dhclient hook already up to date at %s", DHCLIENT_HOOK_PATH)
        return
    DHCLIENT_HOOK_PATH.write_text(desired)
    DHCLIENT_HOOK_PATH.chmod(0o755)
    LOG.info("installed dhclient hook at %s", DHCLIENT_HOOK_PATH)


def cmd_install(name: str) -> int:
    uplinks = load_uplinks()
    uplink = uplinks.get(name)
    if uplink is None:
        LOG.error("no uplink named %r in %s", name, UPLINKS_PATH)
        return 1

    if uplink.client == "dhclient":
        install_dhclient_hook()
        if shutil.which("dhclient") is None:
            LOG.warning("dhclient not found on PATH — install isc-dhcp-client")
    else:
        LOG.error("don't know how to install client %r (only dhclient is "
                   "currently supported for install/run)", uplink.client)
        return 1

    Path("/run/dhclient." + name + ".pid").parent.mkdir(parents=True, exist_ok=True)
    Path("/var/lib/dhcp").mkdir(parents=True, exist_ok=True)

    LOG.info("uplink %r ready: interface=%s router=%s port=%s client=%s",
              name, uplink.interface, uplink.router, uplink.port, uplink.client)
    return 0


def cmd_run(name: str) -> int:
    """
    Long-running supervisor — this is what systemd's ExecStart should be.
    Ensures the hook is installed, then execs the configured client in
    the foreground so systemd supervises the real client process
    directly (journal capture, Restart=, etc. all work normally).
    """
    uplinks = load_uplinks()
    uplink = uplinks.get(name)
    if uplink is None:
        LOG.error("no uplink named %r in %s", name, UPLINKS_PATH)
        return 1

    if uplink.client not in CLIENT_COMMANDS:
        LOG.error("don't know how to run client %r for uplink %r", uplink.client, name)
        return 1

    if uplink.client == "dhclient":
        install_dhclient_hook()

    argv = [
        part.format(name=name, interface=uplink.interface)
        for part in CLIENT_COMMANDS[uplink.client]
    ]
    LOG.info("starting %s for uplink %r on %s", uplink.client, name, uplink.interface)
    LOG.debug("exec: %s", " ".join(argv))

    # replace this process entirely — systemd then supervises the client
    # directly, exactly as if ExecStart had pointed at it from the start.
    os.execv(argv[0], argv)
    return 1  # unreachable if execv succeeds


# ───────────────────────── CLI ─────────────────────────

def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("OVN_UPLINK_SYNC_DEBUG") else logging.INFO,
        format="[ovn-uplink-sync] %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} {{install|run|dhclient|dhcpcd|apply}} [args]",
              file=sys.stderr)
        return 2

    mode = sys.argv[1]

    if mode == "install":
        if len(sys.argv) < 3:
            print("usage: install <uplink-name>", file=sys.stderr)
            return 2
        return cmd_install(sys.argv[2])

    if mode == "run":
        if len(sys.argv) < 3:
            print("usage: run <uplink-name>", file=sys.stderr)
            return 2
        return cmd_run(sys.argv[2])

    if mode == "dhclient":
        event = adapter_dhclient()
        uplinks = load_uplinks()
        apply_lease(event, find_uplink_by_interface(uplinks, event.interface))
        return 0

    if mode == "dhcpcd":
        event = adapter_dhcpcd()
        uplinks = load_uplinks()
        apply_lease(event, find_uplink_by_interface(uplinks, event.interface))
        return 0

    if mode == "apply":
        if len(sys.argv) < 5:
            print("usage: apply <interface> <ip> <prefix> [gateway]", file=sys.stderr)
            return 2
        interface, ip, prefix = sys.argv[2], sys.argv[3], int(sys.argv[4])
        gw = sys.argv[5] if len(sys.argv) > 5 else None
        event = LeaseEvent(interface, "bound", ip, prefix, gw, source="manual")
        uplinks = load_uplinks()
        apply_lease(event, find_uplink_by_interface(uplinks, interface))
        return 0

    print(f"unknown mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
