#!/bin/sh
# Install as: /etc/networkd-dispatcher/configured.d/50-ovn-uplink-sync
# (and symlink/copy into routable.d, off.d, etc. as needed)
# networkd-dispatcher calls hooks with the interface name as $1 and
# exports IFACE/STATE in the environment — forward $1 along.
python3 /usr/local/bin/ovn_uplink_sync.py networkd "$1"
