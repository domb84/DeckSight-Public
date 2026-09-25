#!/bin/bash

set -euo pipefail

ACPI_CALL="/proc/acpi/call"
METHOD='\_SB.PCI0.LPC0.EC0.VFCD.RDDI'
OUTPUT="${1:-anx-registers-$(date +%Y%m%d-%H%M%S).txt}"

if [[ "$(id -u)" -ne 0 ]]; then
    printf 'Run as root: sudo %s [output-file]\n' "$0" >&2
    exit 1
fi

if [[ ! -e "$ACPI_CALL" ]]; then
    printf '%s is unavailable; load the acpi_call module first.\n' "$ACPI_CALL" >&2
    exit 1
fi

printf '# DeckSight ANX7530 read-only snapshot\n' > "$OUTPUT"
printf '# timestamp: %s\n' "$(date --iso-8601=seconds)" >> "$OUTPUT"
printf '# method: %s\n' "$METHOD" >> "$OUTPUT"
printf '# format: register result\n' >> "$OUTPUT"

# RDDI is the firmware's read wrapper: it programs the ANX address, performs
# ANR4, and returns the resulting ANRD value to the caller.
for register in $(seq 0 255); do
    printf '%s 0x%02x\n' "$METHOD" "$register" > "$ACPI_CALL"
    result=$(tr -d '\000' < "$ACPI_CALL" | sed 's/called.*//' | grep -oE '^0x[0-9a-fA-F]+' | head -n 1 || true)
    [[ -n "$result" ]] || result="<no-integer-result>"
    printf '0x%02x %s\n' "$register" "$result" >> "$OUTPUT"
done

printf '%s\n' "$OUTPUT"
