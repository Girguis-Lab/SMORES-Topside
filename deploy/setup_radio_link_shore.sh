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
#      leave this Pi wearing this Pi's campus IP address.
#
# Why NAT rather than a true bridge: "as if connected directly" would mean the
# buoy holding its own address on the campus subnet. That needs the university
# to accept a second MAC/IP on this switch port, which campus NAC / 802.1x /
# port-security setups almost always refuse. NAT needs nothing from the
# university and gives the buoy full outbound internet — which is all Tailscale
# needs, since inbound access arrives over the Tailscale tunnel, not the campus
# network. (If your network admin *will* route you a spare campus address, drop
# the NAT unit and add `proxyarp` to the peers file instead.)
#
# This is deliberately independent of the SMORES backend and of Tailscale: its
# own systemd units, started before them, and neither knows it exists.
#
# BEFORE RUNNING — configure both radios identically with RFD Modem Tools or an
# AT session (`+++` with a second of silence either side, `ATI5` to list,
# `AT&W` to save, `ATZ` to reboot):
#
#   ATS1=57      SERIAL_SPEED in kbps. Must equal --baud below.
#   ATS2=250     AIR_SPEED 250 kbps — the figure quoted for this link. Note that
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
#
# Every setting is also readable from the environment:
#   RADIO_DEV=/dev/serial/by-id/... RADIO_BAUD=115200 \
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
# quarters it, and the 250 kbps air rate can absorb either.
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

PEER_NAME="smores-radio"
PEER_FILE="/etc/ppp/peers/${PEER_NAME}"
NFT_FILE="/etc/nftables.d/smores-radio-nat.nft"
SYSCTL_FILE="/etc/sysctl.d/99-smores-radio-forward.conf"
LINK_UNIT="smores-radio-link.service"
NAT_UNIT="smores-radio-nat.service"

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
  -s, --status            Show link and NAT state, then exit
  -r, --remove            Tear the whole thing back down
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
    [ -e "$RADIO_DEV" ] || warn "$RADIO_DEV does not exist yet; the service will wait for it."
}

require_root() {
    [ "$(id -u)" -eq 0 ] || die "must run as root — try: sudo -E $0 $*"
}

require_tools() {
    [ -e /usr/sbin/pppd ] || die "pppd not found; install it with: apt install ppp"
    command -v nft >/dev/null 2>&1 || die "nft not found; install it with: apt install nftables"
}

# ------------------------------------------------------------ file writing --

write_peer_file() {
    install -d -m 0755 /etc/ppp/peers
    cat >"$PEER_FILE" <<EOF
# Managed by deploy/setup_radio_link_shore.sh — regenerated on every run.
#
# Shore side of the RFD900x link.
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

# local:remote, both pinned. Nothing is negotiated, so either Pi may boot first
# and neither has to be "the server".
$SHORE_IP:$BUOY_IP

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

# This Pi's own default route belongs to the campus ethernet; never let the
# radio link take it.
nodefaultroute

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

# Campus egress here is v4-only, so skip IPV6CP negotiation entirely.
noipv6

# Stay in the foreground so systemd supervises pppd directly.
nodetach

# --- staying up -------------------------------------------------------------
# lcp-echo-* is the only thing that notices a *silent* failure: RF noise, the
# buoy powering off, the buoy rebooting. $LCP_FAILURE unanswered echoes
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

write_nat_files() {
    install -d -m 0755 /etc/sysctl.d
    printf '# Managed by deploy/setup_radio_link_shore.sh\nnet.ipv4.ip_forward=1\n' >"$SYSCTL_FILE"
    note "Wrote $SYSCTL_FILE"

    install -d -m 0755 /etc/nftables.d
    cat >"$NFT_FILE" <<'EOF'
#!/usr/sbin/nft -f
# Managed by deploy/setup_radio_link_shore.sh
#
# Its own table, loaded with the add/delete/add idiom, so applying this replaces
# only these rules and never disturbs a ruleset some other tool installed.

table ip smores_nat
delete table ip smores_nat

table ip smores_nat {
    chain postrouting {
        type nat hook postrouting priority srcnat; policy accept;

        # Anything arriving over a PPP link and leaving by any other interface
        # goes out as this Pi. Matching on the interface rather than on the
        # buoy's address means it keeps working if the link addressing changes,
        # if the uplink moves from eth0 to wlan0, or if a LAN is ever hung off
        # the buoy.
        iifname "ppp*" oifname != "ppp*" masquerade
    }
}
EOF
    note "Wrote $NFT_FILE"
}

write_units() {
    cat >"/etc/systemd/system/$NAT_UNIT" <<EOF
[Unit]
Description=SMORES radio link NAT (masquerade buoy traffic onto the campus network)
Documentation=file:///home/pi/SMORES-Topside/deploy/setup_radio_link_shore.sh
After=network.target
Before=$LINK_UNIT

[Service]
# oneshot + RemainAfterExit: the rules outlive the process that installed them,
# and they are deliberately NOT tied to link state — flapping the NAT table
# every time the radio drops would be pointless churn, and conntrack entries
# for connections riding the link would be lost with it.
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/sysctl -q -w net.ipv4.ip_forward=1
ExecStart=/usr/sbin/nft -f $NFT_FILE
ExecStop=-/usr/sbin/nft delete table ip smores_nat

[Install]
WantedBy=multi-user.target
EOF
    note "Wrote /etc/systemd/system/$NAT_UNIT"

    cat >"/etc/systemd/system/$LINK_UNIT" <<EOF
[Unit]
Description=SMORES radio link (PPP over RFD900x) — shore side
Documentation=man:pppd(8)
Documentation=file:///home/pi/SMORES-Topside/deploy/setup_radio_link_shore.sh
Wants=$NAT_UNIT
After=$NAT_UNIT

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
    note "Wrote /etc/systemd/system/$LINK_UNIT"
}

# ------------------------------------------------------------------ apply --

do_apply() {
    validate_args
    require_tools

    write_peer_file
    write_nat_files
    write_units

    sysctl -q -w net.ipv4.ip_forward=1
    systemctl daemon-reload
    systemctl enable --now "$NAT_UNIT" >/dev/null
    systemctl enable --now "$LINK_UNIT" >/dev/null
    ok "Enabled $NAT_UNIT and $LINK_UNIT (both start at boot)."

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
}

# ----------------------------------------------------------------- status --

do_status() {
    local rc=0
    note "Units:"
    systemctl is-enabled "$NAT_UNIT" "$LINK_UNIT" 2>&1 | sed 's/^/  enabled: /' || true
    systemctl is-active "$NAT_UNIT" "$LINK_UNIT" 2>&1 | sed 's/^/  active:  /' || true

    note "PPP interfaces:"
    if [ -n "$(ppp_addrs)" ]; then
        ppp_addrs | sed 's/^/  /'
    else
        warn "  none — the link is down"
        rc=1
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
        warn "See: journalctl -u $LINK_UNIT -n 50"
    fi
    return "$rc"
}

# ----------------------------------------------------------------- remove --

do_remove() {
    systemctl disable --now "$LINK_UNIT" >/dev/null 2>&1 || true
    systemctl disable --now "$NAT_UNIT" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$LINK_UNIT" "/etc/systemd/system/$NAT_UNIT"
    rm -f "$PEER_FILE" "$NFT_FILE" "$SYSCTL_FILE"
    nft delete table ip smores_nat 2>/dev/null || true
    systemctl daemon-reload
    # ip_forward is left as-is in the running kernel: the sysctl.d file that set
    # it is gone, so it reverts on the next boot. Flipping it off now could cut
    # forwarding that something else on this Pi depends on.
    ok "Removed the shore-side radio link, its NAT table and its units."
    note "net.ipv4.ip_forward stays 1 until the next reboot."
}

# ------------------------------------------------------------------- main --

case "$ACTION" in
    status) do_status ;;
    apply)  require_root "$@"; do_apply ;;
    remove) require_root "$@"; do_remove ;;
esac
