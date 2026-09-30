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

# Resolvers to use once the radio link is the only way out.
#
# Not taken from the peer via `usepeerdns`: that only writes
# /etc/ppp/resolv.conf, and the Debian hook that copies it into the system
# resolver needs `resolvconf`/`openresolv` installed, which the Pi OS image
# does not have. A fixed list is one less moving part on an unattended buoy.
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

PEER_NAME="smores-radio"
PEER_FILE="/etc/ppp/peers/${PEER_NAME}"
LINK_UNIT="smores-radio-link.service"
NM_DNS_FILE="/etc/NetworkManager/conf.d/90-smores-radio-dns.conf"
RESOLVED_DNS_FILE="/etc/systemd/resolved.conf.d/90-smores-radio-dns.conf"
RESOLV_BACKUP="/etc/resolv.conf.smores-radio-backup"

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
    [ -e "$RADIO_DEV" ] || warn "$RADIO_DEV does not exist yet; the service will wait for it."
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root — try: sudo -E $0 $*"
}

require_tools() {
    [ -e /usr/sbin/pppd ] || die "pppd not found; install it with: apt install ppp"
}

# ------------------------------------------------------------ file writing --

write_peer_file() {
    install -d -m 0755 /etc/ppp/peers
    cat >"$PEER_FILE" <<EOF
# Managed by deploy/setup_radio_link_buoy.sh — regenerated on every run.
#
# Buoy side of the RFD900x link.
#
# Every comment below starts at column 0 and takes a whole line. pppd's
# options-file parser documents "lines beginning with #" as comments and
# nothing more, so a trailing comment after an option is not safe to rely on.
#
# Debug a bring-up by hand with:
#   sudo systemctl stop $LINK_UNIT
#   sudo pppd call $PEER_NAME nodetach debug

$RADIO_DEV
$RADIO_BAUD

# local:remote, both pinned — the mirror of the shore side. Nothing is
# negotiated, so either Pi may boot first and neither has to be "the server".
$BUOY_IP:$SHORE_IP

# The next two override Debian's /etc/ppp/options, which pppd reads first and
# which is written for dial-up modems. Both overrides are mandatory:
#
#   local  - /etc/ppp/options sets \`modem\`, so pppd waits for carrier-detect
#            before doing anything. A USB-serial adapter wired to a radio never
#            raises DCD, so without this the link silently never starts.
#   noauth - /etc/ppp/options sets \`auth\`, demanding the peer prove its
#            identity from a secrets file. This is a private two-node link that
#            no third party can reach.
local
noauth

# The radio is this Pi's route to the world, so unlike the shore side it does
# take the default route. replacedefaultroute puts the old one back on hangup,
# which keeps a maintenance ethernet cable working once the link goes down.
defaultroute
replacedefaultroute

# Flow control. See FLOW_CONTROL in the setup script: the RFD900x ships with
# RTS/CTS off, and waiting on a CTS that never comes wedges the port silently.
$FLOW_CONTROL

# The radio is an 8-bit clean pipe, so don't spend air time escaping control
# characters.
asyncmap 0

# Keep 1500. Tailscale's 1280-byte WireGuard packets plus their UDP/IP headers
# must fit in one PPP frame; fragmenting them over a lossy radio turns one lost
# fragment into a lost packet.
mtu 1500
mru 1500

# The shore side NATs v4 only, so skip IPV6CP negotiation entirely.
noipv6

# Stay in the foreground so systemd supervises pppd directly.
nodetach

# --- staying up -------------------------------------------------------------
# lcp-echo-* is the only thing that notices a *silent* failure: RF noise, the
# shore Pi powering off, the shore Pi rebooting. $LCP_FAILURE unanswered echoes
# ${LCP_INTERVAL}s apart tear the link down after ~$((LCP_INTERVAL * LCP_FAILURE))s.
lcp-echo-interval $LCP_INTERVAL
lcp-echo-failure $LCP_FAILURE

# persist + maxfail 0 then renegotiate forever, $HOLDOFF s apart, without pppd
# ever exiting — so a radio outage heals itself with no process restart and no
# systemd churn. The unit's Restart=always is the outer layer, for the case
# pppd genuinely dies (the USB adapter being yanked out).
persist
maxfail 0
holdoff $HOLDOFF

# Uncomment for full LCP/IPCP negotiation traces:
#debug
EOF
    chmod 0644 "$PEER_FILE"
    note "Wrote $PEER_FILE"
}

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

# nolog stops pppd writing every line to both stderr and syslog; journald still
# collects the syslog copy, so 'journalctl -u $LINK_UNIT' shows everything once.
ExecStart=/usr/sbin/pppd call $PEER_NAME nodetach nolog

# The peers file's persist/maxfail already ride out RF outages and peer reboots
# without pppd exiting. This layer is for when it does exit: the USB adapter
# being unplugged, or a SIGTERM from 'systemctl restart'.
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
# Two stacks to cope with. systemd-resolved owns resolution when it is running;
# otherwise NetworkManager writes /etc/resolv.conf directly and has to be told
# to stop, or it will overwrite whatever is put there the next time a carrier
# comes or goes.

configure_dns() {
    [ "$SET_DNS" = "yes" ] || { note "Leaving DNS configuration alone (--no-dns)."; return 0; }

    if systemctl is-active --quiet systemd-resolved; then
        install -d -m 0755 /etc/systemd/resolved.conf.d
        {
            printf '# Managed by deploy/setup_radio_link_buoy.sh\n[Resolve]\n'
            printf 'DNS=%s\n' "$BUOY_DNS"
        } >"$RESOLVED_DNS_FILE"
        systemctl restart systemd-resolved
        note "Set systemd-resolved DNS to: $BUOY_DNS"
        return 0
    fi

    # Stop NetworkManager managing /etc/resolv.conf before writing it, or the
    # file is correct only until the next interface event.
    if systemctl is-active --quiet NetworkManager; then
        install -d -m 0755 /etc/NetworkManager/conf.d
        printf '# Managed by deploy/setup_radio_link_buoy.sh\n[main]\ndns=none\n' >"$NM_DNS_FILE"
        systemctl reload NetworkManager
        note "Told NetworkManager to stop rewriting /etc/resolv.conf ($NM_DNS_FILE)."
    fi

    if [ ! -e "$RESOLV_BACKUP" ] && [ -f /etc/resolv.conf ]; then
        cp -a /etc/resolv.conf "$RESOLV_BACKUP"
        note "Backed up the previous /etc/resolv.conf to $RESOLV_BACKUP"
    fi

    # A dangling or stub symlink has to go before the real file can be written.
    # Spelled as an `if` rather than `[ ... ] && rm`: the latter evaluates to 1
    # when the file is a regular one, which is the common case, and `set -e`
    # would take the script down with it.
    if [ -L /etc/resolv.conf ]; then
        rm -f /etc/resolv.conf
    fi

    {
        printf '# Managed by deploy/setup_radio_link_buoy.sh\n'
        printf '# The radio link is this Pi'"'"'s only route out; these resolvers are\n'
        printf '# reached through the shore Pi'"'"'s NAT.\n'
        for ns in $BUOY_DNS; do printf 'nameserver %s\n' "$ns"; done
        printf 'options timeout:3 attempts:2\n'
    } >/etc/resolv.conf
    chmod 0644 /etc/resolv.conf
    note "Wrote /etc/resolv.conf with: $BUOY_DNS"

    if systemctl is-active --quiet tailscaled; then
        warn "tailscaled is running. With MagicDNS accepted it rewrites"
        warn "/etc/resolv.conf itself, using the file above as its upstream base."
        warn "If name resolution misbehaves, try: tailscale set --accept-dns=false"
    fi
}

restore_dns() {
    rm -f "$RESOLVED_DNS_FILE"
    if systemctl is-active --quiet systemd-resolved; then
        systemctl restart systemd-resolved
    fi
    if [ -f "$NM_DNS_FILE" ]; then
        rm -f "$NM_DNS_FILE"
        if systemctl is-active --quiet NetworkManager; then
            systemctl reload NetworkManager
        fi
        note "Handed /etc/resolv.conf back to NetworkManager."
    fi
    if [ -e "$RESOLV_BACKUP" ]; then
        cp -a "$RESOLV_BACKUP" /etc/resolv.conf
        rm -f "$RESOLV_BACKUP"
        note "Restored the original /etc/resolv.conf."
    fi
}

# ------------------------------------------------------------------ apply --

do_apply() {
    validate_args
    require_tools

    write_peer_file
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

    note "Resolvers in use:"
    if systemctl is-active --quiet systemd-resolved && command -v resolvectl >/dev/null 2>&1; then
        resolvectl dns 2>/dev/null | sed 's/^/  /' || true
    else
        grep -E '^nameserver' /etc/resolv.conf 2>/dev/null | sed 's/^/  /' || warn "  none configured"
    fi
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
    rm -f "/etc/systemd/system/$LINK_UNIT" "$PEER_FILE"
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
