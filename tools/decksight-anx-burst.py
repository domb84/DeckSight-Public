#!/usr/bin/env python3
"""Experimentally switch the DeckSight ANX bridge between non-burst and burst DSI.

r04 programs SW_PANEL_INFO_1 (OCM register 0x01:0xB1) as 0x44: SET_DPHY_TIMING
plus trans_mode 1 (non-burst sync events). The stock LCD firmware used 0x48
(SET_DPHY_TIMING plus trans_mode 2, burst). This tool rewrites that one OCM
register through the EC mailbox, then repeats the EC's own "panel info ready"
notification (0x9F = 0x7B, 0x9E = 0xC0) so the bridge firmware can recompute
the DSI clocks and timing itself.

The change lives only in bridge RAM. The EC reloads its panel table whenever
the bridge powers up, so a full shutdown restores the r04 configuration. See
tools/decksight-anx-burst.md before using --apply.

Slave 0x01 also contains the bridge's OCM flash controller (0x0F-0x3F, 0x60).
Every write therefore goes through a hard (slave, register, value) allowlist
and uses the firmware's single-byte ANW1 transaction, never a 32-bit write.
"""

import argparse
import json
import mmap
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "bios"))
from decksight_bios import R04_SPI  # noqa: E402  (single source of truth for the r04 table)

EC_PAGE = 0xFE700000
XECB_OFFSET = 0xB00
PAGE_SIZE = 0x1000
ANXC = XECB_OFFSET + 0xA4
ANXD = XECB_OFFSET + 0xA5
ANXO = XECB_OFFSET + 0xA6
ANWD = XECB_OFFSET + 0xA7
ANRD = XECB_OFFSET + 0xAB
MAILBOX_BUSY = 0x80
READ8_COMMAND = 0x81
WRITE8_COMMAND = 0x82   # ANW1 in the live DSDT: ANWD = Arg2 & 0xFF, ANXC = 0x82
READ32_COMMAND = 0x83
POLL_INTERVAL_SECONDS = 0.001
POLL_ATTEMPTS = 200

OCM_SLAVE = 0x01
MIPI_CONTROL_SLAVE = 0x08
MIPI_PORT0_SLAVE = 0xC0

SW_PANEL_FRAME_RATE = 0x9D
MISC_NOTIFY_OCM0 = 0x9E
MISC_NOTIFY_OCM1 = 0x9F
SW_PANEL_INFO_0 = 0xB0
SW_PANEL_INFO_1 = 0xB1
MCU_LOAD_DONE = 0x80
PANEL_INFO_SET_DONE = 0x40
NOTIFY_OCM1_VALUE = 0x7B
NOTIFY_OCM0_VALUE = MCU_LOAD_DONE | PANEL_INFO_SET_DONE

PANEL_INFO_1_NON_BURST = 0x44   # r04: SET_DPHY_TIMING | trans_mode 1
PANEL_INFO_1_BURST = 0x48       # stock: SET_DPHY_TIMING | trans_mode 2

# The only writes this tool can ever issue.
ALLOWED_WRITES = {
    (OCM_SLAVE, SW_PANEL_INFO_1): {PANEL_INFO_1_NON_BURST, PANEL_INFO_1_BURST},
    (OCM_SLAVE, MISC_NOTIFY_OCM1): {NOTIFY_OCM1_VALUE},
    (OCM_SLAVE, MISC_NOTIFY_OCM0): {MCU_LOAD_DONE, NOTIFY_OCM0_VALUE},
}

MIPI_TX_STATE = 0x22
DSI_MODE_CFG = 0x34
DSI_VID_MODE_CFG = 0x38
DSI_VID_PKT_SIZE = 0x3C
DSI_VID_HSA_TIME = 0x48
DSI_VID_HBP_TIME = 0x4C
DSI_VID_HLINE_TIME = 0x50
DSI_VID_VSA_LINES = 0x54
DSI_VID_VBP_LINES = 0x58
DSI_VID_VFP_LINES = 0x5C
DSI_VID_VACTIVE_LINES = 0x60
DSI_PHY_STATUS = 0xB0

DSI_REGISTERS = {
    "mode_cfg": DSI_MODE_CFG,
    "vid_mode_cfg": DSI_VID_MODE_CFG,
    "vid_pkt_size": DSI_VID_PKT_SIZE,
    "vid_hsa_time": DSI_VID_HSA_TIME,
    "vid_hbp_time": DSI_VID_HBP_TIME,
    "vid_hline_time": DSI_VID_HLINE_TIME,
    "vid_vsa_lines": DSI_VID_VSA_LINES,
    "vid_vbp_lines": DSI_VID_VBP_LINES,
    "vid_vfp_lines": DSI_VID_VFP_LINES,
    "vid_vactive_lines": DSI_VID_VACTIVE_LINES,
}

VIDEO_MODE_NAMES = {0: "non-burst sync pulses", 1: "non-burst sync events", 2: "burst", 3: "burst"}
SETTLE_SECONDS = 2.0


class Mailbox:
    def __enter__(self):
        self._memory = open("/dev/mem", "r+b", buffering=0)
        self._map = mmap.mmap(
            self._memory.fileno(),
            PAGE_SIZE,
            flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
            offset=EC_PAGE,
        )
        return self

    def __exit__(self, *exc):
        self._map.close()
        self._memory.close()

    def _run(self, command, what):
        if self._map[ANXC] & MAILBOX_BUSY:
            raise RuntimeError(f"EC mailbox already busy before {what}; another client is using it")
        self._map[ANXC] = command
        for _ in range(POLL_ATTEMPTS):
            if not self._map[ANXC] & MAILBOX_BUSY:
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"EC mailbox stayed busy for 200 ms during {what}")

    def read(self, slave, register, width=1):
        self._map[ANRD:ANRD + 4] = b"\0\0\0\0"
        self._map[ANXD] = slave
        self._map[ANXO] = register
        self._run(READ8_COMMAND if width == 1 else READ32_COMMAND,
                  f"read 0x{slave:02x}:0x{register:02x}")
        return int.from_bytes(self._map[ANRD:ANRD + width], "little")

    def write8(self, slave, register, value):
        allowed = ALLOWED_WRITES.get((slave, register))
        if allowed is None or value not in allowed:
            raise PermissionError(
                f"refusing write 0x{slave:02x}:0x{register:02x} = 0x{value:02x}: not on the allowlist"
            )
        # Same field order as the firmware's ANW1 method.
        self._map[ANWD:ANWD + 4] = b"\0\0\0\0"
        self._map[ANXD] = slave
        self._map[ANXO] = register
        self._map[ANWD:ANWD + 4] = (value & 0xFF).to_bytes(4, "little")
        self._run(WRITE8_COMMAND, f"write 0x{slave:02x}:0x{register:02x}")


def brightness_controller_active():
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "decksight-brightnessctrl.service"],
        check=False,
    )
    return result.returncode == 0


def snapshot(mailbox):
    ocm = {reg: mailbox.read(OCM_SLAVE, reg) for reg, _ in R04_SPI}
    dsi = {name: mailbox.read(MIPI_PORT0_SLAVE, reg, width=4) for name, reg in DSI_REGISTERS.items()}
    return {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "ocm": ocm,
        "dsi": dsi,
        "dsi_phy_status": mailbox.read(MIPI_PORT0_SLAVE, DSI_PHY_STATUS),
        "mipi_tx_state": mailbox.read(MIPI_CONTROL_SLAVE, MIPI_TX_STATE),
    }


def video_mode(vid_mode_cfg):
    return VIDEO_MODE_NAMES[vid_mode_cfg & 0x3]


def print_snapshot(snap, prefix="  "):
    ocm, dsi = snap["ocm"], snap["dsi"]
    info1 = ocm[SW_PANEL_INFO_1]
    notify0 = ocm[MISC_NOTIFY_OCM0]
    print(f"{prefix}SW_PANEL_INFO_1 (0x01:0xb1): 0x{info1:02x} "
          f"(trans_mode {(info1 >> 2) & 0x3}, SET_DPHY_TIMING {'on' if info1 & 0x40 else 'off'})")
    print(f"{prefix}SW_PANEL_FRAME_RATE (0x01:0x9d): {ocm[SW_PANEL_FRAME_RATE]}")
    print(f"{prefix}MISC_NOTIFY_OCM1/0 (0x01:0x9f/0x9e): 0x{ocm[MISC_NOTIFY_OCM1]:02x}/0x{notify0:02x} "
          f"(PANEL_INFO_SET_DONE {'set' if notify0 & PANEL_INFO_SET_DONE else 'clear'})")
    print(f"{prefix}DSI video mode (0xc0:0x38): 0x{dsi['vid_mode_cfg']:08x} ({video_mode(dsi['vid_mode_cfg'])})")
    print(f"{prefix}DSI mode / packet size (0xc0:0x34/0x3c): "
          f"0x{dsi['mode_cfg']:08x} / 0x{dsi['vid_pkt_size']:08x}")
    print(f"{prefix}DSI hsa/hbp/hline: {dsi['vid_hsa_time']}/{dsi['vid_hbp_time']}/{dsi['vid_hline_time']}")
    print(f"{prefix}DSI vsa/vbp/vfp/vactive: {dsi['vid_vsa_lines']}/{dsi['vid_vbp_lines']}/"
          f"{dsi['vid_vfp_lines']}/{dsi['vid_vactive_lines']}")
    # phy_lock (bit 0) reads 0 on r04 while video runs, so it is likely not wired
    # on this PHY; compare this byte before/after rather than trusting bit 0.
    phy = snap["dsi_phy_status"]
    print(f"{prefix}DSI PHY status (0xc0:0xb0): 0x{phy:02x} "
          f"(lock bit {phy & 1}, clock lane {'ULPS' if not phy & 0x08 else 'active'}, "
          f"lane 0 {'ULPS' if not phy & 0x20 else 'active'})")
    print(f"{prefix}MIPI TX state (0x08:0x22): 0x{snap['mipi_tx_state']:02x} "
          f"({'stable' if snap['mipi_tx_state'] & 1 else 'NOT stable'})")


def table_mismatches(snap):
    """r04 table registers that do not hold their firmware value (notify registers excluded)."""
    skip = {SW_PANEL_INFO_1, MISC_NOTIFY_OCM0, MISC_NOTIFY_OCM1}
    return [(reg, want, snap["ocm"][reg]) for reg, want in R04_SPI
            if reg not in skip and snap["ocm"][reg] != want]


def planned_writes(target, rearm):
    writes = [(OCM_SLAVE, SW_PANEL_INFO_1, target)]
    if rearm:
        writes.append((OCM_SLAVE, MISC_NOTIFY_OCM0, MCU_LOAD_DONE))
    # Same order as the end of the EC's panel table.
    writes.append((OCM_SLAVE, MISC_NOTIFY_OCM1, NOTIFY_OCM1_VALUE))
    writes.append((OCM_SLAVE, MISC_NOTIFY_OCM0, NOTIFY_OCM0_VALUE))
    return writes


def append_log(path, record):
    if path:
        with open(path, "a") as log:
            log.write(json.dumps(record) + "\n")


def confirm(word):
    print()
    print("Recovery if the panel goes dark or garbled: hold the power button ~10 s to force off,")
    print("or `sudo poweroff` over SSH, then power on. A full shutdown reloads the r04 table.")
    print("Do not suspend between applying and judging the result; resume may reload the table.")
    try:
        answer = input(f"Type '{word}' to send the writes: ")
    except EOFError:
        answer = ""
    return answer.strip() == word


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("status", "burst", "restore"),
                        help="status: read only; burst: 0x44 -> 0x48; restore: 0x48 -> 0x44")
    parser.add_argument("--apply", action="store_true",
                        help="actually send the writes (default is a dry run)")
    parser.add_argument("--rearm", action="store_true",
                        help="clear PANEL_INFO_SET_DONE before re-notifying, so the OCM sees a 0 -> 1 edge")
    parser.add_argument("--yes", action="store_true", help="skip the interactive confirmation")
    parser.add_argument("--log", metavar="PATH", help="append JSON before/after snapshots to PATH")
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("Run as root: sudo python3 tools/decksight-anx-burst.py ...", file=sys.stderr)
        return 1
    if brightness_controller_active():
        print("Stop decksight-brightnessctrl.service first; it shares the EC mailbox.", file=sys.stderr)
        return 2

    try:
        with Mailbox() as mailbox:
            before = snapshot(mailbox)
            print("Current state:")
            print_snapshot(before)

            if args.action == "status":
                append_log(args.log, {"action": "status", "snapshot": before})
                return 0

            mismatches = table_mismatches(before)
            if mismatches:
                print("\nRefusing: the bridge's panel table does not match r04:", file=sys.stderr)
                for reg, want, got in mismatches:
                    print(f"  0x01:0x{reg:02x} expected 0x{want:02x}, read 0x{got:02x}", file=sys.stderr)
                return 4

            expected, target = ((PANEL_INFO_1_NON_BURST, PANEL_INFO_1_BURST) if args.action == "burst"
                                else (PANEL_INFO_1_BURST, PANEL_INFO_1_NON_BURST))
            current = before["ocm"][SW_PANEL_INFO_1]
            if current not in (expected, target):
                print(f"\nRefusing: SW_PANEL_INFO_1 is 0x{current:02x}, expected 0x{expected:02x} "
                      f"(or 0x{target:02x} from an earlier run) before '{args.action}'.", file=sys.stderr)
                return 4

            writes = planned_writes(target, args.rearm)
            if current == target:
                # An earlier run already stored the value; only re-send the notification.
                writes = writes[1:]
            print("\nPlanned single-byte writes:")
            for slave, reg, value in writes:
                print(f"  0x{slave:02x}:0x{reg:02x} = 0x{value:02x}")
            if not args.rearm and before["ocm"][MISC_NOTIFY_OCM0] & PANEL_INFO_SET_DONE:
                print("  note: PANEL_INFO_SET_DONE is already set, so re-writing it may not be seen "
                      "by the OCM; --rearm clears it first")

            if not args.apply:
                print("\nDry run only; nothing was written. Add --apply to send these writes.")
                return 0
            if not args.yes and not confirm(args.action):
                print("Aborted; nothing was written.")
                return 0

            for slave, reg, value in writes:
                mailbox.write8(slave, reg, value)
                print(f"  wrote 0x{slave:02x}:0x{reg:02x} = 0x{value:02x}")

            readback = mailbox.read(OCM_SLAVE, SW_PANEL_INFO_1)
            time.sleep(SETTLE_SECONDS)
            after = snapshot(mailbox)
    except (OSError, RuntimeError, TimeoutError, PermissionError) as error:
        print(f"Mailbox error: {error}", file=sys.stderr)
        return 3

    print(f"\nState {SETTLE_SECONDS:.0f} s after the writes:")
    print_snapshot(after)
    append_log(args.log, {"action": args.action, "rearm": args.rearm, "writes": writes,
                          "before": before, "after": after})

    changed = [name for name in DSI_REGISTERS if before["dsi"][name] != after["dsi"][name]]
    print()
    if readback != target:
        print(f"Result: SW_PANEL_INFO_1 read back 0x{readback:02x}, not 0x{target:02x}; the write did not stick.")
    elif not changed:
        print("Result: the register took, but the DSI host was not reprogrammed. The OCM did not re-run")
        print("panel init on this notification" + ("" if args.rearm else "; --rearm is the next step") + ".")
    else:
        print(f"Result: the OCM reprogrammed the DSI host ({', '.join(changed)} changed); "
              f"video mode is now {video_mode(after['dsi']['vid_mode_cfg'])}.")
        print("Check the panel now, then run modeset reproduction from this state without suspending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
