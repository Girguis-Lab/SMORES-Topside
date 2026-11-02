### Q: Can the SIK radios work with PPP while being a half-duplex connection without hardware flow control (nocrtscts setting)?

__AI Answer:__
 PPP over SiK works with nocrtscts. You don't need any PPP setting to stop the radios talking over each other, because the SiK firmware already handles that. crtscts fixes a different problem, which is buffer overflow between the Pi and its own radio.

Who prevents talking over each other
SiK radios share the air using time slots (TDM) plus frequency hopping. The two radios take turns transmitting on the air. The serial side is full-duplex: each Pi can write at any time, and the radio holds the bytes until its next slot.

So PPP, which is full-duplex, can run on top of it without changes. No setting in pppd or Linux has any effect on who transmits on the air.

What crtscts actually does
RTS/CTS only covers the short wire between each Pi and its own radio. When the radio's serial buffer is nearly full, it drops CTS and the Pi stops sending.

Without it, the failure looks like this:

The Pi sends a burst, such as a CSV download or a TCP window of data.
The radio can't drain it fast enough, because it only transmits during its own slots.
The buffer overflows and bytes are silently dropped.
The damaged PPP frame fails its checksum and is thrown away.
TCP or Tailscale has to resend it.
Nothing breaks, but under bulk traffic throughput drops and latency jumps around.

The comment at setup_radio_link_shore.sh:81 is too optimistic. It says that at 57600 baud there is nothing for flow control to protect, which isn't always true. Real one-way throughput falls well below the 250 kbps air rate for two reasons:

Error correction: turning ECC on (ATS5=1) roughly halves the data rate.
Shared air time: traffic in both directions splits the slots, minus hopping and preamble overhead.
With both of those, one direction can drop below 57.6 kbps, so overruns can happen even at the default baud. At 115200 or higher they are likely without flow control.

Recommendations, in order of preference
Use crtscts if your cable carries RTS/CTS. The RFD900x's 6-pin FTDI-style header has CTS and RTS pins. Check that your USB adapter actually wires both before enabling this, or the port will block forever. Set ATS14=1 and AT&W on both radios.

If you stay on nocrtscts, add traffic shaping on ppp0. Limiting the rate a bit below the real air throughput stops Linux from overrunning the radio. It also keeps SSH responsive during bulk transfers. Something like this, run from an /etc/ppp/ip-up.d/ hook since ppp0 is recreated on every reconnect:


tc qdisc replace dev ppp0 root cake bandwidth 40kbit
# or: tbf rate 40kbit burst 1600 latency 400ms
Find the rate by testing: run iperf3 over the link and use about 80% of what it measures in each direction.

Turn off PPP compression with noccp. Tailscale traffic is already encrypted, so it can't be compressed. On a lossy link, every lost frame would also trigger a compression reset. Header compression (VJ) is harmless either way.

Radio settings worth checking (same on both radios)
Register	Setting	Why
S6 MAVLINK	0	Already in the script. Raw mode, so the radio doesn't try to parse PPP as MAVLink.
S5 ECC	0 for throughput, 1 for noisy RF	PPP's checksum already catches corrupted frames. ECC trades half the bandwidth for fewer lost frames.
S11 DUTY_CYCLE	100	Anything lower throttles the radio.
S12 LBT_RSSI	0	Listen-before-talk adds delay and isn't needed in the US band.
S15 MAX_WINDOW	low, e.g. 33	Already in the script. Shorter slots mean lower latency.
S14 RTSCTS	must match --flow-control
I can correct the line 81 comment and add noccp plus an optional ip-up.d shaping hook to both setup scripts if you'd like.
