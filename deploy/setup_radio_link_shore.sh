#!/usr/bin/env bash
#
# setup_radio_link_shore.sh — SHORE-SIDE half of the RFD900x radio bridge.
#
# Topology this belongs to:
#
#   university ethernet ── [SHORE PI] ── RFD900x "A" ))) ((( RFD900x "B" ── [BUOY PI]
#                          (this script)                         (setup_radio_link_buoy.sh)
#
# What it sets up, and why each piece:
#
#   1. pppd over the radio's serial port. A SiK radio is a transparent byte
#      pipe, not an ethernet device, so there is nothing to bridge at layer 2 —
#      PPP is the standard way to carry IP over a serial byte pipe, it is in
#      Debian already, and it has the two things that matter here built in:
#      liveness detection (LCP echo) and unattended redial (`persist`).
#
#   2. IPv4 forwarding plus an nftables masquerade rule, so the buoy's packets
#      leave this Pi wearing this Pi's campus IP address. Both are applied as
#      plain `sysctl -w` / `nft add` commands by a oneshot unit at every boot —
#      nothing in /etc/sysctl.d or /etc/nftables*, so the unit file is the one
#      place that defines them, and removing it removes them.
#
# Why NAT rather than a true bridge: "as if connected directly" would mean the
# buoy holding its own address on the campus subnet. That needs the university
# to accept a second MAC/IP on this switch port, which campus NAC / 802.1x /
# port-security setups almost always refuse. NAT needs nothing from the
# university and gives the buoy full outbound internet — which is all Tailscale
# needs, since inbound access arrives over the Tailscale tunnel, not the campus
# network. (If your network admin *will* route you a spare campus address, drop
# the NAT unit and add `proxyarp` to the pppd options in the link unit.)
#
# This is deliberately independent of the SMORES backend and of Tailscale: its
# own systemd units, started before them, and neither knows it exists.
#
# BEFORE RUNNING — configure both radios identically with RFD Modem Tools or an
# AT session (`+++` with a second of silence either side, `ATI5` to list,
# `AT&W` to save, `ATZ` to reboot):
#
#   ATS1=57      SERIAL_SPEED in kbps. Must equal --baud below.
#   ATS2=224     AIR_SPEED 224 kbps — the figure quoted for this link. Note that
#                is the *air* rate; real throughput is roughly half of it, and
#                S1 (the wire to the Pi) is a separate, lower number by default.
#   ATS3=<id>    NETID — same on both radios, different from anyone else nearby.
#   ATS6=0       MAVLINK off. The default framing mode inspects the byte stream
#                for MAVLink packets; PPP frames are not MAVLink, so keep it RAW.
#   ATS14=0      RTSCTS. See --flow-control below.
#   ATS15=33     MAX_WINDOW ms (default 131). Lower is much better latency for
#                interactive traffic, at some cost in peak throughput.
#
# Usage:
#   sudo deploy/setup_radio_link_shore.sh --device /dev/serial/by-id/usb-FTDI_...
#   sudo deploy/setup_radio_link_shore.sh --status
#   sudo deploy/setup_radio_link_shore.sh --remove
#   sudo deploy/setup_radio_link_shore.sh --remove-tailscale-mtu
#
# Every setting is also readable from the environment:
#   RADIO_DEV=/dev/serial/by-id/... RADIO_BAUD=115200 PPP_MTU=576 PPP_MRU=576 \
#     sudo -E deploy/setup_radio_link_shore.sh

set -euo pipefail

# ---------------------------------------------------------------- defaults --

# No default device on purpose. This Pi may also have RS485 adapters plugged in,
# and pointing pppd at a dissolved-oxygen sensor bus would be a bad afternoon.
RADIO_DEV="${RADIO_DEV:-}"

# Must match the radio's S1:SERIAL_SPEED. SiK accepts 2/4/9/19/38/57/115/230/
# 460/1000 kbps; the matching pppd values are the ones in SPEEDS below.
# 57600 is the RFD900x factory default.
#
# Worth raising: at 57600 a full 1500-byte packet takes 260 ms just to clock
# into the radio, which is felt directly as SSH lag. 115200 halves that, 230400
# quarters it, and the 224 kbps air rate can absorb either.
RADIO_BAUD="${RADIO_BAUD:-57600}"

# `nocrtscts` by default because the RFD900x ships with RTS/CTS *disabled*
# (S14=0), and asking the kernel to wait for a CTS that never arrives wedges
# the port with no error message — a miserable thing to debug in the field.
#
# At the 57600 default the serial wire is slower than the air link, so there is
# nothing for flow control to protect. Above that the radio's buffer can
# overrun: set ATS14=1 on BOTH radios and re-run with --flow-control crtscts.
FLOW_CONTROL="${FLOW_CONTROL:-nocrtscts}"

# The point-to-point link's own addresses. Fixed on both ends rather than
# negotiated, so neither Pi has to be the one that boots first.
SHORE_IP="${SHORE_IP:-10.55.0.1}"
BUOY_IP="${BUOY_IP:-10.55.0.2}"

# How long a dead radio link goes unnoticed: LCP_FAILURE unanswered echoes,
# LCP_INTERVAL seconds apart. 6 x 10 s means the link is torn down and
# renegotiated after ~60 s of silence. Shorter reacts faster but drops the link
# during RF fades that would have recovered on their own.
LCP_INTERVAL="${LCP_INTERVAL:-10}"
LCP_FAILURE="${LCP_FAILURE:-6}"

# Seconds between redial attempts once the link is down.
HOLDOFF="${HOLDOFF:-5}"

# Largest IP packet the PPP link sends (MTU) and accepts (MRU), in bytes.
# Smaller packets mean less air time is lost when the radio drops a frame, but
# more header overhead per byte of payload. Each side's MRU limits what the
# other side sends, so set the same values on both Pis.
PPP_MTU="${PPP_MTU:-576}"
PPP_MRU="${PPP_MRU:-$PPP_MTU}"

# MTU of tailscaled's tailscale0 interface, set through TS_DEBUG_MTU in a
# systemd drop-in. Every tunnel packet gets 60 bytes of wrapping (20 IPv4 + 8
# UDP + 32 WireGuard), so the default is the largest size that still fits in
# one PPP frame, min(PPP_MTU, PPP_MRU) - 60, capped at Tailscale's own 1280
# default. Empty means use that default. Only matters here if this Pi's own
# tailnet traffic is sent to the buoy over the radio; Tailscale traffic from
# this Pi to the internet goes out over ethernet.
TS_MTU="${TS_MTU:-}"
SET_TS_MTU="${SET_TS_MTU:-yes}"
WG_OVERHEAD=60
TS_DROPIN="/etc/systemd/system/tailscaled.service.d/smores-radio-mtu.conf"

NFT_FILE="/etc/nftables.d/smores-radio-nat.nft"
SYSCTL_FILE="/etc/sysctl.d/99-smores-radio-forward.conf"
SMORES_LINK_SERVICE="smores-radio-link.service"
SMORES_NAT_SERVICE="smores-radio-nat.service"

ACTION="apply"
PROG="${0##*/}"

# pppd maps a baud number onto a termios B-constant from a fixed table; a rate
# that is not in it is rejected outright. These are the ones that exist.
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
$PROG — bring up the shore-side PPP-over-radio bridge and NAT the buoy onto
the university network.

Options:
  -d, --device PATH       Radio serial port, ideally a stable
                          /dev/serial/by-id/... path      (required)
  -b, --baud RATE         Serial speed; must equal the radio's S1
                                                          (default: $RADIO_BAUD)
      --flow-control MODE crtscts | nocrtscts             (default: $FLOW_CONTROL)
                          crtscts requires ATS14=1 on BOTH radios
      --shore-ip ADDR     This Pi's address on the link   (default: $SHORE_IP)
      --buoy-ip ADDR      The buoy's address on the link  (default: $BUOY_IP)
      --mtu BYTES         PPP MTU; match the buoy side    (default: $PPP_MTU)
      --mru BYTES         PPP MRU; match the buoy side    (default: same as --mtu)
      --tailscale-mtu BYTES
                          tailscaled TS_DEBUG_MTU         (default: smaller of
                                                          MTU/MRU minus $WG_OVERHEAD, max 1280)
      --no-tailscale-mtu  Leave tailscaled's MTU alone
  -s, --status            Show link and NAT state, then exit
  -r, --remove            Tear the whole thing back down, including
                          tailscaled's MTU
      --remove-tailscale-mtu
                          Only restore tailscaled's default MTU
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
        --shore-ip)       SHORE_IP="${2:?--shore-ip needs a value}"; shift 2 ;;
        --buoy-ip)        BUOY_IP="${2:?--buoy-ip needs a value}"; shift 2 ;;
        --mtu)            PPP_MTU="${2:?--mtu needs a value}"; shift 2 ;;
        --mru)            PPP_MRU="${2:?--mru needs a value}"; shift 2 ;;
        --tailscale-mtu)  TS_MTU="${2:?--tailscale-mtu needs a value}"; SET_TS_MTU="yes"; shift 2 ;;
        --no-tailscale-mtu) SET_TS_MTU="no"; shift ;;
        -s|--status)      ACTION="status"; shift ;;
        -r|--remove)      ACTION="remove"; shift ;;
        --remove-tailscale-mtu) ACTION="remove-ts-mtu"; shift ;;
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
        die "--device is required (pick the RFD900x's adapter from the list above)"
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
        *) warn "$RADIO_DEV is not a /dev/serial/by-id path. ttyUSB numbers are"
           warn "assigned in probe order, so this will point somewhere else the"
           warn "first time the Pi boots with the adapters plugged in differently." ;;
    esac
    # The path is written straight into the unit's ExecStart=, where systemd
    # would read % as a specifier and whitespace as an argument break.
    case "$RADIO_DEV" in
        *%*|*[[:space:]]*) die "$RADIO_DEV contains % or whitespace; use a path without them" ;;
    esac
    [ -e "$RADIO_DEV" ] || warn "$RADIO_DEV does not exist yet; the service will wait for it."
    validate_mtu
}

is_uint() { case "$1" in ''|*[!0-9]*) return 1 ;; esac; }

# Also fills in the default TS_MTU, which depends on the PPP values.
validate_mtu() {
    local opt val
    for opt in mtu:"$PPP_MTU" mru:"$PPP_MRU"; do
        val="${opt#*:}"
        { is_uint "$val" && [ "$val" -ge 128 ] && [ "$val" -le 1500 ]; } \
            || die "--${opt%%:*} must be a whole number of bytes from 128 to 1500 (got '$val')"
    done
    local ppp_min=$(( PPP_MTU < PPP_MRU ? PPP_MTU : PPP_MRU ))
    if [ -z "$TS_MTU" ]; then
        TS_MTU=$(( ppp_min - WG_OVERHEAD ))
        [ "$TS_MTU" -le 1280 ] || TS_MTU=1280
    fi
    { is_uint "$TS_MTU" && [ "$TS_MTU" -ge 68 ]; } \
        || die "--tailscale-mtu must be a whole number of bytes, at least 68 (got '$TS_MTU')"
    [ $(( TS_MTU + WG_OVERHEAD )) -le "$ppp_min" ] \
        || warn "Tailscale MTU $TS_MTU + $WG_OVERHEAD bytes of tunnel overhead exceeds the PPP MTU/MRU $ppp_min; every full-size tunnel packet will be fragmented."
    [ "$TS_MTU" -ge 1280 ] \
        || warn "Tailscale MTU $TS_MTU is below 1280: IPv6 inside the tailnet stops working (IPv4 100.x addresses are fine)."
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root — try: sudo -E $0 $*"
}

require_tools() {
    [ -e /usr/sbin/pppd ] || die "pppd not found; install it with: apt install ppp"
    command -v nft >/dev/null 2>&1 || die "nft not found; install it with: apt install nftables"
}

# ------------------------------------------------------------ file writing --

write_units() {
    cat >"/etc/systemd/system/$SMORES_NAT_SERVICE" <<EOF
[Unit]
Description=SMORES radio link NAT (masquerade buoy traffic onto the campus network)
Documentation=file:///home/pi/SMORES-Topside/deploy/setup_radio_link_shore.sh
After=network.target
Before=$SMORES_RADIO_LINK_SERVICE
# nftables.service, if enabled, runs 'flush ruleset' on start/restart, which
# would silently wipe the table below while this unit still reports active.
# PartOf re-runs this unit whenever that happens; harmless if it is not enabled.
After=nftables.service
PartOf=nftables.service

[Service]
# oneshot + RemainAfterExit: the rules outlive the process that installed them,
# and they are deliberately NOT tied to link state — flapping the NAT table
# every time the radio drops would be pointless churn, and conntrack entries
# for connections riding the link would be lost with it.
Type=oneshot
RemainAfterExit=yes

# Runtime only; nothing in /etc/sysctl.d. Reverts to 0 on reboot unless this
# unit runs again.
ExecStart=/usr/sbin/sysctl -q -w net.ipv4.ip_forward=1

# Create our own nft table rather than the shared 'ip nat' one, which iptables-nft, Docker
# and Tailscale also write into — so stop can drop exactly our rules and
# nothing else. 'nft add rule' appends, so start from an empty table every
# time; otherwise each restart would stack another copy of the rule.
ExecStartPre=-/usr/sbin/nft delete table ip smores_nat
ExecStart=/usr/sbin/nft add table ip smores_nat
ExecStart=/usr/sbin/nft add chain ip smores_nat postrouting '{ type nat hook postrouting priority srcnat; policy accept; }'
# Anything arriving over a PPP link and leaving by any other interface goes out
# as this Pi. iifname, not oifname: it is the buoy's traffic *from* the radio
# that needs this Pi's campus address. Matching on interface rather than the
# buoy's IP keeps working if the link addressing changes, if the uplink moves
# from eth0 to wlan0, or if a LAN is ever hung off the buoy.
ExecStart=/usr/sbin/nft add rule ip smores_nat postrouting iifname "ppp*" oifname != "ppp*" masquerade

ExecStop=-/usr/sbin/nft delete table ip smores_nat

[Install]
WantedBy=multi-user.target
EOF
    note "Wrote /etc/systemd/system/$SMORES_NAT_SERVICE"

    cat >"/etc/systemd/system/$SMORES_RADIO_LINK_SERVICE" <<EOF
[Unit]
Description=SMORES radio link (PPP over RFD900x) — shore side
Documentation=man:pppd(8)
Documentation=file:///home/pi/SMORES-Topside/deploy/setup_radio_link_shore.sh
Wants=$SMORES_NAT_SERVICE
After=$SMORES_NAT_SERVICE

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
#   sudo systemctl edit $SMORES_RADIO_LINK_SERVICE      ->  [Service]
#                                           Environment=PPPD_EXTRA=debug
Environment=PPPD_EXTRA=

# Every pppd option lives on this command line — there is no /etc/ppp/peers
# file. pppd still reads Debian's /etc/ppp/options first; everything here
# overrides it. systemd drops the comment lines between continued lines.
ExecStart=/usr/sbin/pppd $RADIO_DEV $RADIO_BAUD \\
# local:remote, both pinned. Nothing is negotiated, so either Pi may boot first
# and neither has to be "the server".
    $SHORE_IP:$BUOY_IP \\
# Both override /etc/ppp/options, which is written for dial-up modems:
#   local  - it sets 'modem', so pppd waits for carrier-detect. A USB-serial
#            adapter wired to a radio never raises DCD, so without this the
#            link silently never starts.
#   noauth - it sets 'auth', demanding the peer prove its identity from a
#            secrets file. This is a private two-node link.
    local noauth \\
# This Pi's default route belongs to the campus ethernet; never let the radio
# link take it.
    nodefaultroute \\
# RTS/CTS. The RFD900x ships with it off (ATS13=0), and waiting on a CTS that
# never comes wedges the port silently. crtscts needs ATS13=1 on BOTH radios.
    $FLOW_CONTROL \\
# The radio is an 8-bit clean pipe; don't spend air time escaping control
# characters.
    asyncmap 0 \\
# PPP_MTU / PPP_MRU. tailscaled's MTU is set $WG_OVERHEAD bytes smaller (see
# $TS_DROPIN) so each tunnel packet fits in one PPP frame; over a lossy radio,
# losing one fragment loses the whole packet.
    mtu $PPP_MTU mru $PPP_MRU \\
# Campus egress here is v4-only, so skip IPV6CP negotiation entirely.
    noipv6 \\
# lcp-echo-* is the only thing that notices a *silent* failure: RF noise, the
# buoy powering off or rebooting. $LCP_FAILURE unanswered echoes ${LCP_INTERVAL} s apart
# tear the link down after ~$((LCP_INTERVAL * LCP_FAILURE)) s.
    lcp-echo-interval $LCP_INTERVAL lcp-echo-failure $LCP_FAILURE \\
# persist + maxfail 0 renegotiate forever, $HOLDOFF s apart, without pppd ever
# exiting — so a radio outage heals itself with no process restart.
    persist maxfail 0 holdoff $HOLDOFF \\
# nodetach keeps pppd in the foreground so systemd supervises it directly.
# nolog stops it writing every line to both stderr and syslog; journald keeps
# the syslog copy, so 'journalctl -u $SMORES_RADIO_LINK_SERVICE' shows everything once.
    nodetach nolog \$PPPD_EXTRA

# persist/maxfail above already ride out RF outages and peer reboots without
# pppd exiting. This layer is for when it does exit: the USB adapter being
# unplugged, or a SIGTERM from 'systemctl restart'.
Restart=always
RestartSec=5

# pppd turns SIGTERM into a clean LCP terminate, so the peer learns the link is
# going away instead of having to time it out.
KillSignal=SIGTERM
TimeoutStopSec=15

StandardOutput=journal
StandardError=journal
SyslogIdentifier=smores-radio-link

[Install]
WantedBy=multi-user.target
EOF
    note "Wrote /etc/systemd/system/$SMORES_RADIO_LINK_SERVICE"
}

# ------------------------------------------------------------------ apply --

do_apply() {
    validate_args
    require_tools
    write_units

    systemctl daemon-reload
    # restart, not just start: on a re-run the unit is already active, and a
    # oneshot only re-applies its rules when it is actually run again.
    systemctl enable "$SMORES_NAT_SERVICE" >/dev/null
    systemctl restart "$SMORES_NAT_SERVICE"
    systemctl enable --now "$SMORES_RADIO_LINK_SERVICE" >/dev/null
    ok "Enabled $SMORES_NAT_SERVICE and $SMORES_RADIO_LINK_SERVICE (both start at boot)."

    printf '\n'
    if [ "$FLOW_CONTROL" = "crtscts" ]; then
        warn "Hardware flow control is on. Both radios need ATS14=1 (then AT&W) or"
        warn "the port blocks forever waiting for a CTS that never comes."
    elif [ "$RADIO_BAUD" -gt 57600 ]; then
        warn "Baud $RADIO_BAUD with no flow control: the radio's buffer can overrun"
        warn "and corrupt frames. Set ATS14=1 on both radios and re-run with"
        warn "--flow-control crtscts."
    fi
    note "Give the link ~30 s to negotiate, then: sudo $0 --status"
    printf '\n'
    do_status || true

    # Last, because the tailscaled restart may drop a Tailscale SSH session.
    # Revert with --remove-tailscale-mtu.
    printf '\n'
    configure_tailscale_mtu
}

# ----------------------------------------------------------------- status --

do_status() {
    local rc=0
    note "Units:"
    systemctl is-enabled "$SMORES_NAT_SERVICE" "$SMORES_RADIO_LINK_SERVICE" 2>&1 | sed 's/^/  enabled: /' || true
    systemctl is-active "$SMORES_NAT_SERVICE" "$SMORES_RADIO_LINK_SERVICE" 2>&1 | sed 's/^/  active:  /' || true

    note "PPP interfaces:"
    if [ -n "$(ppp_addrs)" ]; then
        ppp_addrs | sed 's/^/  /'
    else
        warn "  none — the link is down"
        rc=1
    fi

    note "MTUs:"
    ip -oneline link show 2>/dev/null | awk '$2 ~ /^(ppp|tailscale)/ { sub(":", "", $2); print "  " $2 " mtu " $5 }' || true
    if [ -f "$TS_DROPIN" ]; then
        grep -h 'TS_DEBUG_MTU' "$TS_DROPIN" | sed "s|^|  $TS_DROPIN: |"
    else
        note "  tailscaled is using its default MTU (no $TS_DROPIN)"
    fi

    note "IPv4 forwarding: $(cat /proc/sys/net/ipv4/ip_forward)"

    note "NAT table:"
    if nft list table ip smores_nat 2>/dev/null | grep -q masquerade; then
        nft list table ip smores_nat | sed 's/^/  /' || true
    else
        warn "  table ip smores_nat is not loaded"
        rc=1
    fi

    note "Reaching the buoy at $BUOY_IP:"
    if ping -c 2 -W 3 "$BUOY_IP" >/dev/null 2>&1; then
        ok "  $BUOY_IP responds"
    else
        warn "  no reply from $BUOY_IP"
        rc=1
    fi

    if [ "$rc" -eq 0 ]; then
        ok "Shore side is up."
    else
        warn "See: journalctl -u $SMORES_RADIO_LINK_SERVICE -n 50"
    fi
    return "$rc"
}

# ----------------------------------------------------------------- remove --

do_remove() {
    systemctl disable --now "$SMORES_RADIO_LINK_SERVICE" >/dev/null 2>&1 || true
    systemctl disable --now "$SMORES_NAT_SERVICE" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$SMORES_RADIO_LINK_SERVICE" "/etc/systemd/system/$SMORES_NAT_SERVICE"
    nft delete table ip smores_nat 2>/dev/null || true
    systemctl daemon-reload
    restore_tailscale_mtu
    # ip_forward is left as-is in the running kernel: the sysctl.d file that set
    # it is gone, so it reverts on the next boot. Flipping it off now could cut
    # forwarding that something else on this Pi depends on.
    ok "Removed the shore-side radio link, its NAT table and its units."
    note "net.ipv4.ip_forward stays 1 until the next reboot."
}

# ---------------------------------------------------------- tailscale MTU --
#
# The same thing as `systemctl edit tailscaled` with
#   [Service]
#   Environment=TS_DEBUG_MTU=<n>
# but in a drop-in file of its own instead of override.conf, so reverting it
# never touches edits anyone made by hand. Revert with --remove-tailscale-mtu
# (or --remove), or by hand:
#   sudo rm $TS_DROPIN
#   sudo systemctl daemon-reload && sudo systemctl restart tailscaled
#
# The restart is queued with --no-block and done last. If this script is run
# over a Tailscale SSH session, that session drops for a few seconds while
# tailscaled restarts, and the script must not be in the middle of anything.

configure_tailscale_mtu() {
    [ "$SET_TS_MTU" = "yes" ] || { note "Leaving tailscaled's MTU alone (--no-tailscale-mtu)."; return 0; }
    if ! systemctl cat tailscaled.service >/dev/null 2>&1; then
        warn "tailscaled.service is not installed, so its MTU was not set. Re-run"
        warn "this script after installing Tailscale."
        return 0
    fi

    local want
    want="$(printf '# Managed by deploy/%s. Revert with: sudo %s --remove-tailscale-mtu\n[Service]\nEnvironment=TS_DEBUG_MTU=%s\n' "$PROG" "$0" "$TS_MTU")"
    if [ -f "$TS_DROPIN" ] && [ "$(cat "$TS_DROPIN")" = "$want" ]; then
        note "tailscaled MTU is already $TS_MTU ($TS_DROPIN)."
        return 0
    fi

    install -d -m 0755 "${TS_DROPIN%/*}"
    printf '%s\n' "$want" >"$TS_DROPIN"
    systemctl daemon-reload
    note "Wrote $TS_DROPIN (TS_DEBUG_MTU=$TS_MTU); restarting tailscaled if it is running."
    systemctl --no-block try-restart tailscaled.service
}

restore_tailscale_mtu() {
    [ -f "$TS_DROPIN" ] || { note "tailscaled MTU was not changed by this script; nothing to revert."; return 0; }
    rm -f "$TS_DROPIN"
    rmdir "${TS_DROPIN%/*}" 2>/dev/null || true
    systemctl daemon-reload
    note "Deleted $TS_DROPIN; restarting tailscaled with its default MTU if it is running."
    systemctl --no-block try-restart tailscaled.service
}

# ------------------------------------------------------------------- main --

case "$ACTION" in
    status) do_status ;;
    apply)  require_root "$@"; do_apply ;;
    remove) require_root "$@"; do_remove ;;
    remove-ts-mtu) require_root "$@"; restore_tailscale_mtu ;;
esac
