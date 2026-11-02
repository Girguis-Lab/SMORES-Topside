#!/usr/bin/env bash

DEVICE="/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A946T3BG-if00-port0"
OUTPUT_FILE="./readable_modbus_registers.txt"

for ((reg=1; reg<=9000; reg++)); do
    tmp_file=$(mktemp)

    echo "Scanning ${reg}..."

    if modbus "$DEVICE" \
        -s 1 \
        --stop-bits 1 \
        --baud 19200 \
        --parity e \
        --byte-order be \
        --timeout 0.1 \
        -v "h@${reg}/h" \
        >"$tmp_file" 2>/dev/null
    then
        if ! grep -qF "Invalid address" "$tmp_file"; then
            cat "$tmp_file" | tee -a "$OUTPUT_FILE"
        fi
    fi

    rm -f "$tmp_file"
    sleep 0.01
done
