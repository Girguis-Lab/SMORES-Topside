#!/usr/bin/env bash
#
# setup_radio_link_buoy.sh — BUOY-SIDE half of the RFD900x radio bridge.
#
# Topology this belongs to:
#
#   university ethernet ── [SHORE PI] ── RFD900x "A" ))) ((( RFD900x "B" ── [BUOY PI]
#                    (setup_radio_link_shore.sh)                          (this script)
#
# Run the shore script on the shore Pi first, then this one here. This side is
# the simpler half: it brings up the same PPP link, points its default route
# down the radio, and makes sure DNS resolves. All the NAT lives on the shore.
#
# Deliberately independent of the SMORES backend and of Tailscale. Its own
# systemd unit, ordered before them, and neither knows it exists — Tailscale
# just finds working internet where there was none, and reconnects by itself
# (it keeps trying its control plane and DERP relays indefinitely, so a radio
# outage needs no intervention on the Tailscale side at all).
#
# RADIO SETTINGS: both radios must be configured identically — see the "BEFORE
# RUNNING" block at the top of setup_radio_link_shore.sh for the AT commands.
# The critical ones are ATS1 (serial speed, must equal --baud here), ATS3
# (NETID, matching), ATS6=0 (MAVLink framing OFF) and matching ATS2/ATS14.
#
# Usage:
#   sudo deploy/setup_radio_link_buoy.sh --device /dev/serial/by-id/usb-FTDI_...
#   sudo deploy/setup_radio_link_buoy.sh --status
#   sudo deploy/setup_radio_link_buoy.sh --remove
#
# Every setting is also readable from the environment:
#   RADIO_DEV=/dev/serial/by-id/... RADIO_BAUD=115200 \
#     sudo -E deploy/setup_radio_link_buoy.sh

set -euo pipefail

# ---------------------------------------------------------------- defaults --

# No default device on purpose: this Pi has the RS485 sensor adapters plugged
# into it too, and pointing pppd at a dissolved-oxygen bus would be a bad
# afternoon. Always a /dev/serial/by-id/... path here, never a ttyUSB number.
RADIO_DEV="${RADIO_DEV:-}"

# Must match the radio's S1:SERIAL_SPEED and the shore side's --baud.
# 57600 is the RFD900x factory default.
RADIO_BAUD="${RADIO_BAUD:-57600}"

# See the long note in setup_radio_link_shore.sh. Short version: the RFD900x
# ships with RTS/CTS off, and waiting for a CTS that never comes wedges the
# port silently, so the default here is off too. Above 57600 baud, enable
# ATS14=1 on both radios and pass --flow-control crtscts to both scripts.
FLOW_CONTROL="${FLOW_CONTROL:-nocrtscts}"

# Must be the mirror image of the shore script's values.
BUOY_IP="${BUOY_IP:-10.55.0.2}"
SHORE_IP="${SHORE_IP:-10.55.0.1}"

LCP_INTERVAL="${LCP_INTERVAL:-10}"
LCP_FAILURE="${LCP_FAILURE:-6}"
HOLDOFF="${HOLDOFF:-5}"

# Resolvers to use once the radio link is the only way out, installed through
# NetworkManager (see the DNS section below for how, and why not `usepeerdns`).
#
# If the university blocks outbound port 53 to public resolvers, either put the
# campus resolvers here, or run a forwarder on the shore Pi and point this at
# $SHORE_IP:
#   # on the SHORE Pi, one unit, forwards to whatever the shore Pi itself uses:
#   sudo systemd-run --unit=smores-radio-dns --collect \
#     /usr/sbin/dnsmasq -k --interface=ppp0 --bind-dynamic --listen-address=10.55.0.1
#   # then here:
#   sudo deploy/setup_radio_link_buoy.sh --dns 10.55.0.1 --device ...
BUOY_DNS="${BUOY_DNS:-1.1.1.1 9.9.9.9}"
SET_DNS="${SET_DNS:-yes}"

LINK_UNIT="smores-radio-link.service"
DNS_CON="smores-radio-dns"
DNS_IFACE="smoresdns0"

ACTION="apply"
PROG="${0##*/}"

SPEEDS="2400 4800 9600 19200 38400 57600 115200 230400 460800 500000 576000 921600 1000000"

# ----------------------------------------------------------------- output --

note() { printf '\033[0;36m%s\033[0m\n' "$*"; }
ok()   { printf '\033[0;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[0;33mwarning: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[0;31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# `ip addr show type ppp` looks like the right thing and is not: iproute2
# silently ignores an unrecognised `type` for `addr show` and lists every
# interface instead, so that spelling reports the link as up even when no PPP
# interface exists at all. Match on the interface name in field 2 of the
# -oneline output, which is what actually distinguishes a ppp device.
ppp_addrs() { ip -4 -oneline addr show 2>/dev/null | awk '$2 ~ /^ppp/' || true; }

usage() {
    cat <<EOF
$PROG — bring up the buoy-side PPP-over-radio link and route this Pi's
internet access through it.

Options:
  -d, --device PATH       Radio serial port, a stable
                          /dev/serial/by-id/... path      (required)
  -b, --baud RATE         Serial speed; must equal the radio's S1 and the
                          shore side's --baud             (default: $RADIO_BAUD)
      --flow-control MODE crtscts | nocrtscts             (default: $FLOW_CONTROL)
      --buoy-ip ADDR      This Pi's address on the link   (default: $BUOY_IP)
      --shore-ip ADDR     The shore Pi's link address     (default: $SHORE_IP)
      --dns "A [B ...]"   Resolvers to install            (default: $BUOY_DNS)
      --no-dns            Leave this Pi's DNS config alone
  -s, --status            Show link, route, DNS and internet reachability
  -r, --remove            Tear the link back down and restore DNS
  -h, --help              This message

Must be run as root, except for --status.
EOF
}

# ------------------------------------------------------------ arg parsing --

while [ $# -gt 0 ]; do
    case "$1" in
        -d|--device)      RADIO_DEV="${2:?--device needs a value}"; shift 2 ;;
        -b|--baud)        RADIO_BAUD="${2:?--baud needs a value}"; shift 2 ;;
        --flow-control)   FLOW_CONTROL="${2:?--flow-control needs a value}"; shift 2 ;;
        --buoy-ip)        BUOY_IP="${2:?--buoy-ip needs a value}"; shift 2 ;;
        --shore-ip)       SHORE_IP="${2:?--shore-ip needs a value}"; shift 2 ;;
        --dns)            BUOY_DNS="${2:?--dns needs a value}"; SET_DNS="yes"; shift 2 ;;
        --no-dns)         SET_DNS="no"; shift ;;
        -s|--status)      ACTION="status"; shift ;;
        -r|--remove)      ACTION="remove"; shift ;;
        -h|--help)        usage; exit 0 ;;
        *)                usage >&2; die "unknown argument: $1" ;;
    esac
done

# ------------------------------------------------------------- validation --

list_serial_candidates() {
    if [ -d /dev/serial/by-id ] && [ -n "$(ls -A /dev/serial/by-id 2>/dev/null)" ]; then
        printf 'Serial devices currently attached:\n' >&2
        ls -1 /dev/serial/by-id | sed 's|^|  /dev/serial/by-id/|' >&2 || true
    else
        printf 'No USB serial devices are attached right now.\n' >&2
    fi
}

validate_args() {
    if [ -z "$RADIO_DEV" ]; then
        list_serial_candidates
        die "--device is required (pick the RFD900x's adapter, NOT an RS485 sensor bus)"
    fi
    case " $SPEEDS " in
        *" $RADIO_BAUD "*) ;;
        *) die "pppd cannot use baud $RADIO_BAUD; supported rates are: $SPEEDS" ;;
    esac
    case "$FLOW_CONTROL" in
        crtscts|nocrtscts) ;;
        *) die "--flow-control must be crtscts or nocrtscts" ;;
    esac
    case "$RADIO_DEV" in
        /dev/serial/by-id/*) ;;
        *) warn "$RADIO_DEV is not a /dev/serial/by-id path. With the RS485 sensor"
           warn "adapters on this Pi too, ttyUSB numbers get reshuffled on reboot." ;;
    esac
    # The path is written straight into the unit's ExecStart=, where systemd
    # would read % as a specifier and whitespace as an argument break.
    case "$RADIO_DEV" in
        *%*|*[[:space:]]*) die "$RADIO_DEV contains % or whitespace; use a path without them" ;;
    esac
    [ -e "$RADIO_DEV" ] || warn "$RADIO_DEV does not exist yet; the service will wait for it."
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root — try: sudo -E $0 $*"
}

require_tools() {
    [ -e /usr/sbin/pppd ] || die "pppd not found; install it with: apt install ppp"
    [ "$SET_DNS" = "no" ] || command -v nmcli >/dev/null 2>&1 \
        || die "nmcli not found; install network-manager or re-run with --no-dns"
}

# ------------------------------------------------------------ file writing --

write_unit() {
    cat >"/etc/systemd/system/$LINK_UNIT" <<EOF
[Unit]
Description=SMORES radio link (PPP over RFD900x) — buoy side
Documentation=man:pppd(8)
Documentation=file:///home/pi/SMORES-Topside/deploy/setup_radio_link_buoy.sh

# Before the things that want internet. Neither is a hard dependency: the buoy
# must still serve sensor data locally with the radio link down.
Before=tailscaled.service smores-topside.service

# No start rate limit. This is an unattended field link: if the radio is
# unplugged for ten minutes, systemd must still be retrying when it comes back,
# not latched into 'failed' after the default 5 starts in 10 s.
StartLimitIntervalSec=0

[Service]
Type=exec

# Wait up to 20 s for the adapter rather than failing instantly at boot, when
# USB enumeration often has not finished yet. (No shell variables inside, on
# purpose — systemd expands \$name itself before /bin/sh ever sees it.)
ExecStartPre=/bin/sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do [ -e "$RADIO_DEV" ] && exit 0; sleep 2; done; echo "radio serial device $RADIO_DEV never appeared" >&2; exit 1'

# Extra pppd options, empty by default. To trace LCP/IPCP negotiation without
# touching this file (which the setup script regenerates):
#   sudo systemctl edit $LINK_UNIT      ->  [Service]
#                                           Environment=PPPD_EXTRA=debug
Environment=PPPD_EXTRA=

# Every pppd option lives on this command line — there is no /etc/ppp/peers
# file. pppd still reads Debian's /etc/ppp/options first; everything here
# overrides it. systemd drops the comment lines between continued lines.
ExecStart=/usr/sbin/pppd $RADIO_DEV $RADIO_BAUD \\
# local:remote, both pinned — the mirror of the shore side. Nothing is
# negotiated, so either Pi may boot first and neither has to be "the server".
    $BUOY_IP:$SHORE_IP \\
# Both override /etc/ppp/options, which is written for dial-up modems:
#   local  - it sets 'modem', so pppd waits for carrier-detect. A USB-serial
#            adapter wired to a radio never raises DCD, so without this the
#            link silently never starts.
#   noauth - it sets 'auth', demanding the peer prove its identity from a
#            secrets file. This is a private two-node link.
    local noauth \\
# The radio is this Pi's route to the world, so unlike the shore side it takes
# the default route. replacedefaultroute puts the old one back on hangup, which
# keeps a maintenance ethernet cable working once the link goes down.
    defaultroute replacedefaultroute \\
# RTS/CTS. The RFD900x ships with it off (ATS14=0), and waiting on a CTS that
# never comes wedges the port silently. crtscts needs ATS14=1 on BOTH radios.
    $FLOW_CONTROL \\
# The radio is an 8-bit clean pipe; don't spend air time escaping control
# characters.
    asyncmap 0 \\
# Keep 1500. Tailscale's 1280-byte WireGuard packets plus UDP/IP headers must
# fit in one PPP frame; over a lossy radio, one lost fragment loses the packet.
    mtu 1500 mru 1500 \\
# The shore side NATs v4 only, so skip IPV6CP negotiation entirely.
    noipv6 \\
# lcp-echo-* is the only thing that notices a *silent* failure: RF noise, the
# shore Pi powering off or rebooting. $LCP_FAILURE unanswered echoes ${LCP_INTERVAL} s apart
# tear the link down after ~$((LCP_INTERVAL * LCP_FAILURE)) s.
    lcp-echo-interval $LCP_INTERVAL lcp-echo-failure $LCP_FAILURE \\
# persist + maxfail 0 renegotiate forever, $HOLDOFF s apart, without pppd ever
# exiting — so a radio outage heals itself with no process restart.
    persist maxfail 0 holdoff $HOLDOFF \\
# nodetach keeps pppd in the foreground so systemd supervises it directly.
# nolog stops it writing every line to both stderr and syslog; journald keeps
# the syslog copy, so 'journalctl -u $LINK_UNIT' shows everything once.
    nodetach nolog \$PPPD_EXTRA

# persist/maxfail above already ride out RF outages and peer reboots without
# pppd exiting. This layer is for when it does exit: the USB adapter being
# unplugged, or a SIGTERM from 'systemctl restart'.
Restart=always
RestartSec=5

KillSignal=SIGTERM
TimeoutStopSec=15

StandardOutput=journal
StandardError=journal
SyslogIdentifier=smores-radio-link

[Install]
WantedBy=multi-user.target
EOF
    note "Wrote /etc/systemd/system/$LINK_UNIT"
}

# --------------------------------------------------------------------- DNS --
#
# NetworkManager owns /etc/resolv.conf on this Pi (with Tailscale's MagicDNS
# layered on top when enabled), so the resolvers are handed to NetworkManager
# rather than written into the file, where it would overwrite them on the next
# interface event.
#
# NetworkManager only uses a connection's DNS servers while that connection is
# active, and ppp0 is created by pppd, not NetworkManager, so it cannot carry
# them. Putting them on the eth0 profile would lose them whenever no cable is
# plugged in — the normal state out on the buoy. So they get a connection of
# their own: a dummy interface, always up because it has no carrier to lose,
# that exists only to hold the DNS servers:
#
#   never-default  no default route, so it cannot compete with ppp0
#   /32 address    link-local, no subnet route; routes nothing but itself
#
# The dummy only decides WHICH servers get asked. The queries themselves are
# ordinary UDP packets to e.g. 1.1.1.1, so the kernel sends them out the
# default route, which is ppp0 whenever the link is up (see
# `ip route get 1.1.1.1`).
#
# Not valid with systemd-resolved: it binds each interface's DNS servers to
# that interface, so queries would be sent into the dummy and go nowhere.
# Pi OS does not run it, so the script refuses rather than handle that case.
#
# Not `usepeerdns` either: Debian's /etc/ppp/ip-up.d/0000usepeerdns rewrites
# /etc/resolv.conf directly and restores a backup on hangup, fighting both
# NetworkManager and Tailscale over the same file on every radio dropout.

dns_con_exists() { nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$DNS_CON"; }

configure_dns() {
    [ "$SET_DNS" = "yes" ] || { note "Leaving DNS configuration alone (--no-dns)."; return 0; }

    systemctl is-active --quiet NetworkManager \
        || die "NetworkManager is not running; set DNS by hand or re-run with --no-dns"
    ! systemctl is-active --quiet systemd-resolved \
        || die "systemd-resolved is running; the dummy-connection DNS setup does not work under it (re-run with --no-dns)"

    # nmcli takes a comma-separated list; unquoted $BUOY_DNS collapses any
    # run of spaces first.
    local dns_list
    dns_list="$(printf '%s\n' $BUOY_DNS | paste -sd, -)"

    if dns_con_exists; then
        nmcli connection modify "$DNS_CON" ipv4.dns "$dns_list"
    else
        nmcli connection add type dummy con-name "$DNS_CON" ifname "$DNS_IFACE" \
            ipv4.method manual ipv4.addresses 169.254.55.1/32 ipv4.never-default yes \
            ipv4.dns "$dns_list" ipv6.method disabled connection.autoconnect yes >/dev/null
    fi
    nmcli connection up "$DNS_CON" >/dev/null
    note "Set DNS to $BUOY_DNS via NetworkManager connection '$DNS_CON' ($DNS_IFACE)."

    if systemctl is-active --quiet tailscaled; then
        note "tailscaled is running; with MagicDNS on it forwards non-tailnet names"
        note "to these servers. If name resolution misbehaves, try:"
        note "  tailscale set --accept-dns=false"
    fi
}

restore_dns() {
    if dns_con_exists; then
        nmcli connection delete "$DNS_CON" >/dev/null
        note "Deleted NetworkManager connection '$DNS_CON'."
    fi
}

# ------------------------------------------------------------------ apply --

do_apply() {
    validate_args
    require_tools

    write_unit
    configure_dns

    systemctl daemon-reload
    systemctl enable --now "$LINK_UNIT" >/dev/null
    ok "Enabled $LINK_UNIT (starts at boot)."

    printf '\n'
    if [ "$FLOW_CONTROL" = "crtscts" ]; then
        warn "Hardware flow control is on. Both radios need ATS14=1 (then AT&W) or"
        warn "the port blocks forever waiting for a CTS that never comes."
    elif [ "$RADIO_BAUD" -gt 57600 ]; then
        warn "Baud $RADIO_BAUD with no flow control: the radio's buffer can overrun"
        warn "and corrupt frames. Set ATS14=1 on both radios and re-run both"
        warn "scripts with --flow-control crtscts."
    fi
    note "Give the link ~30 s to negotiate, then: sudo $0 --status"
    printf '\n'
    do_status || true
}

# ----------------------------------------------------------------- status --

do_status() {
    local rc=0
    note "Unit:"
    systemctl is-enabled "$LINK_UNIT" 2>&1 | sed 's/^/  enabled: /' || true
    systemctl is-active "$LINK_UNIT" 2>&1 | sed 's/^/  active:  /' || true

    note "PPP interfaces:"
    if [ -n "$(ppp_addrs)" ]; then
        ppp_addrs | sed 's/^/  /'
    else
        warn "  none — the link is down"
        rc=1
    fi

    note "Default route:"
    ip -4 route show default | sed 's/^/  /' || true
    if ip -4 route show default | grep -q 'dev ppp'; then
        ok "  default route goes over the radio"
    else
        warn "  default route is NOT over the radio"
        rc=1
    fi

    note "Reaching the shore Pi at $SHORE_IP:"
    if ping -c 2 -W 3 "$SHORE_IP" >/dev/null 2>&1; then
        ok "  $SHORE_IP responds"
    else
        warn "  no reply from $SHORE_IP"
        rc=1
    fi

    note "Reaching the internet (through the shore Pi's NAT):"
    if ping -c 2 -W 8 1.1.1.1 >/dev/null 2>&1; then
        ok "  1.1.1.1 responds"
    else
        warn "  1.1.1.1 does not respond — check the shore Pi's NAT unit"
        rc=1
    fi

    note "DNS queries to 1.1.1.1 would leave by:"
    ip -4 route get 1.1.1.1 2>/dev/null | head -1 | sed 's/^/  /' || true

    note "Resolvers in use:"
    if [ -n "$(nmcli -g IP4.DNS device show "$DNS_IFACE" 2>/dev/null)" ]; then
        nmcli -g IP4.DNS device show "$DNS_IFACE" | sed 's/ | /\n/g' | sed "s/^/  $DNS_CON: /"
    else
        warn "  NetworkManager connection '$DNS_CON' is not up"
    fi
    grep -E '^nameserver' /etc/resolv.conf 2>/dev/null | sed 's|^|  /etc/resolv.conf: |' || true
    if getent hosts controlplane.tailscale.com >/dev/null 2>&1; then
        ok "  DNS resolves controlplane.tailscale.com"
    else
        warn "  DNS cannot resolve controlplane.tailscale.com — Tailscale will not connect"
        rc=1
    fi

    if [ "$rc" -eq 0 ]; then
        ok "Buoy side is up and has internet."
    else
        warn "See: journalctl -u $LINK_UNIT -n 50"
    fi
    return "$rc"
}

# ----------------------------------------------------------------- remove --

do_remove() {
    systemctl disable --now "$LINK_UNIT" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$LINK_UNIT"
    systemctl daemon-reload
    restore_dns
    ok "Removed the buoy-side radio link and its unit."
}

# ------------------------------------------------------------------- main --

case "$ACTION" in
    status) do_status ;;
    apply)  require_root "$@"; do_apply ;;
    remove) require_root "$@"; do_remove ;;
esac
