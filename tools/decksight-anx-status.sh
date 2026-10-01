#!/bin/bash

set -euo pipefail

ACPI_CALL="/proc/acpi/call"
READ_METHOD='\_SB.PCI0.LPC0.EC0.VFCD.ANR1'
RESULT_FIELD='\_SB.PCI0.LPC0.EC0.ANRD'

if [[ "$(id -u)" -ne 0 ]]; then
    printf 'Run as root: sudo bash %s\n' "$0" >&2
    exit 1
fi

if command -v systemctl >/dev/null 2>&1 && \
        systemctl is-active --quiet decksight-brightnessctrl.service; then
    printf 'Stop decksight-brightnessctrl.service before probing the shared EC mailbox.\n' >&2
    exit 3
fi

if [[ ! -e "$ACPI_CALL" ]]; then
    printf '%s is unavailable; this probe will not load a module.\n' "$ACPI_CALL" >&2
    exit 1
fi

if [[ "${DECKSIGHT_CONFIRM_UNBOUNDED_ANR_READ:-}" != "YES" ]]; then
    printf '%s\n' \
        'WARNING: ANR1 polls an EC busy bit without a timeout.' \
        'A stuck transaction may block this ACPI call.' \
        'No MIPI register is written; this reads MIPI TX state 0x08:0x22.' \
        'To explicitly accept that risk, run with DECKSIGHT_CONFIRM_UNBOUNDED_ANR_READ=YES.' >&2
    exit 2
fi

call_acpi() {
    printf '%s\n' "$1" > "$ACPI_CALL"
    cat "$ACPI_CALL"
}

printf 'ANR1 call status: '
call_acpi "$READ_METHOD 0x08 0x22"
printf '\nANRD result: '
call_acpi "$RESULT_FIELD"
printf '\n'