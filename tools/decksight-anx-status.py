#!/usr/bin/env python3
"""Read the DeckSight ANX MIPI transmitter state through the EC mailbox.

The firmware exposes the ANX register mailbox at physical address
0xFE700B00. This client reads documented MIPI transmitter and DesignWare DSI
registers. It does not send DCS commands or change bridge/panel configuration
unless --panel-dcs-read is explicitly supplied. The optional error-latch read
acknowledges the bridge's latched DSI errors, so use it once per bad-state
reproduction and record its output.
"""

import argparse
import mmap
import os
import subprocess
import sys
import time

EC_PAGE = 0xFE700000
XECB_OFFSET = 0xB00
PAGE_SIZE = 0x1000
ANXC = XECB_OFFSET + 0xA4
ANXD = XECB_OFFSET + 0xA5
ANXO = XECB_OFFSET + 0xA6
ANWD = XECB_OFFSET + 0xA7
ANRD = XECB_OFFSET + 0xAB
READ8_COMMAND = 0x81
READ32_COMMAND = 0x83
WRITE32_COMMAND = 0x84
MIPI_CONTROL_SLAVE = 0x08
MIPI_PORT0_SLAVE = 0xC0
MIPI_TX_SELECT = 0x19
MIPI_TX_STATE = 0x22
MIPI_TX_INTERRUPT = 0x23
DSI_CMD_PKT_STATUS = 0x74
DSI_PHY_STATUS = 0xB0
DSI_INT_STATUS0 = 0xBC
DSI_INT_STATUS1 = 0xC0
DSI_DPI_COLOR_CODING = 0x10
DSI_DPI_CFG_POL = 0x14
DSI_DPI_LP_CMD_TIM = 0x18
DSI_PCKHDL_CFG = 0x2C
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
DSI_CMD_MODE_CFG = 0x68
DSI_GEN_HDR = 0x6C
DSI_GEN_PLD_DATA = 0x70
DSI_TO_CNT_CFG = 0x78
DSI_BTA_TO_CNT = 0x8C
DSI_LPCLK_CTRL = 0x94
DCS_MAXIMUM_RETURN_PACKET_HEADER = 0x00000137
DCS_SHORT_READ_NO_PARAMETER = 0x06
DCS_READ_COMMANDS = {0x06, 0x07, 0x08, 0x0A, 0x0C, 0x0D, 0x0F}
POLL_INTERVAL_SECONDS = 0.001
POLL_ATTEMPTS = 200


def brightness_controller_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", "decksight-brightnessctrl.service"],
        check=False,
    )
    return result.returncode == 0


def read_anx_register(slave: int, register: int, width: int = 1) -> int:
    if width not in (1, 4):
        raise ValueError(f"unsupported register width: {width}")

    with open("/dev/mem", "r+b", buffering=0) as memory:
        mailbox = mmap.mmap(
            memory.fileno(),
            PAGE_SIZE,
            flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
            offset=EC_PAGE,
        )
        try:
            # This is the ANR1 transaction from the firmware DSDT, with a
            # bounded wait unlike its ACPI method's unbounded polling loop.
            mailbox[ANRD:ANRD + 4] = b"\0\0\0\0"
            mailbox[ANXD] = slave
            mailbox[ANXO] = register
            mailbox[ANXC] = READ8_COMMAND if width == 1 else READ32_COMMAND

            for _ in range(POLL_ATTEMPTS):
                if not mailbox[ANXC] & 0x80:
                    return int.from_bytes(mailbox[ANRD:ANRD + width], "little")
                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            mailbox.close()

    raise TimeoutError(
        f"EC mailbox stayed busy for 200 ms reading slave 0x{slave:02x}, "
        f"register 0x{register:02x}"
    )


def write_anx_register32(slave: int, register: int, value: int) -> None:
    with open("/dev/mem", "r+b", buffering=0) as memory:
        mailbox = mmap.mmap(
            memory.fileno(),
            PAGE_SIZE,
            flags=mmap.MAP_SHARED,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
            offset=EC_PAGE,
        )
        try:
            mailbox[ANXD] = slave
            mailbox[ANXO] = register
            mailbox[ANWD:ANWD + 4] = value.to_bytes(4, "little")
            mailbox[ANXC] = WRITE32_COMMAND

            for _ in range(POLL_ATTEMPTS):
                if not mailbox[ANXC] & 0x80:
                    return
                time.sleep(POLL_INTERVAL_SECONDS)
        finally:
            mailbox.close()

    raise TimeoutError(
        f"EC mailbox stayed busy for 200 ms writing slave 0x{slave:02x}, "
        f"register 0x{register:02x}"
    )


def dcs_read_command(value: str) -> int:
    try:
        command = int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer such as 0x0a") from error
    if command not in DCS_READ_COMMANDS:
        raise argparse.ArgumentTypeError("must be one of 0x06, 0x07, 0x08, 0x0a, 0x0c, 0x0d, 0x0f")
    return command


def read_panel_dcs_status(command: int) -> int:
    # Match firmware RDDI exactly, except 0xC0 corrects its invalid 0x0C target.
    write_anx_register32(MIPI_PORT0_SLAVE, DSI_GEN_HDR, DCS_MAXIMUM_RETURN_PACKET_HEADER)
    write_anx_register32(
        MIPI_PORT0_SLAVE,
        DSI_GEN_HDR,
        (command << 8) | DCS_SHORT_READ_NO_PARAMETER,
    )
    time.sleep(0.020)
    return read_anx_register(MIPI_PORT0_SLAVE, DSI_GEN_PLD_DATA, width=4)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--error-latches",
        action="store_true",
        help="read and acknowledge DSI error latches once",
    )
    parser.add_argument(
        "--video-config",
        action="store_true",
        help="read DSI pixel-format and video-mode configuration",
    )
    parser.add_argument(
        "--panel-dcs-read",
        type=dcs_read_command,
        metavar="COMMAND",
        help="send the firmware RDDI sequence (known to alter panel scan state) and read response",
    )
    arguments = parser.parse_args()

    if os.geteuid() != 0:
        print("Run as root: sudo python3 tools/decksight-anx-status.py", file=sys.stderr)
        return 1

    if brightness_controller_active():
        print(
            "Stop decksight-brightnessctrl.service before accessing the shared EC mailbox.",
            file=sys.stderr,
        )
        return 2

    if arguments.panel_dcs_read is not None:
        try:
            dcs_response = read_panel_dcs_status(arguments.panel_dcs_read)
        except (OSError, OverflowError, TimeoutError) as error:
            print(f"Could not read panel DCS status: {error}", file=sys.stderr)
            return 3
        print(
            f"Panel DCS response (0x{arguments.panel_dcs_read:02x}): "
            f"0x{dcs_response:08x}"
        )

    try:
        tx_select = read_anx_register(MIPI_CONTROL_SLAVE, MIPI_TX_SELECT)
        tx_state = read_anx_register(MIPI_CONTROL_SLAVE, MIPI_TX_STATE)
        tx_interrupt = read_anx_register(MIPI_CONTROL_SLAVE, MIPI_TX_INTERRUPT)
        command_status = read_anx_register(MIPI_PORT0_SLAVE, DSI_CMD_PKT_STATUS)
        phy_status = read_anx_register(MIPI_PORT0_SLAVE, DSI_PHY_STATUS)
        interrupt_status0 = (
            read_anx_register(MIPI_PORT0_SLAVE, DSI_INT_STATUS0, width=4)
            if arguments.error_latches
            else None
        )
        interrupt_status1 = (
            read_anx_register(MIPI_PORT0_SLAVE, DSI_INT_STATUS1, width=4)
            if arguments.error_latches
            else None
        )
        video_config = (
            {
                "dpi_color_coding": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_DPI_COLOR_CODING, width=4
                ),
                "dpi_cfg_pol": read_anx_register(MIPI_PORT0_SLAVE, DSI_DPI_CFG_POL, width=4),
                "dpi_lp_cmd_tim": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_DPI_LP_CMD_TIM, width=4
                ),
                "pckhdl_cfg": read_anx_register(MIPI_PORT0_SLAVE, DSI_PCKHDL_CFG, width=4),
                "mode_cfg": read_anx_register(MIPI_PORT0_SLAVE, DSI_MODE_CFG, width=4),
                "vid_mode_cfg": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_VID_MODE_CFG, width=4
                ),
                "vid_pkt_size": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_VID_PKT_SIZE, width=4
                ),
                "cmd_mode_cfg": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_CMD_MODE_CFG, width=4
                ),
                "to_cnt_cfg": read_anx_register(MIPI_PORT0_SLAVE, DSI_TO_CNT_CFG, width=4),
                "bta_to_cnt": read_anx_register(MIPI_PORT0_SLAVE, DSI_BTA_TO_CNT, width=4),
                "lpclk_ctrl": read_anx_register(MIPI_PORT0_SLAVE, DSI_LPCLK_CTRL, width=4),
                "vid_hsa_time": read_anx_register(MIPI_PORT0_SLAVE, DSI_VID_HSA_TIME, width=4),
                "vid_hbp_time": read_anx_register(MIPI_PORT0_SLAVE, DSI_VID_HBP_TIME, width=4),
                "vid_hline_time": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_VID_HLINE_TIME, width=4
                ),
                "vid_vsa_lines": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_VID_VSA_LINES, width=4
                ),
                "vid_vbp_lines": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_VID_VBP_LINES, width=4
                ),
                "vid_vfp_lines": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_VID_VFP_LINES, width=4
                ),
                "vid_vactive_lines": read_anx_register(
                    MIPI_PORT0_SLAVE, DSI_VID_VACTIVE_LINES, width=4
                ),
            }
            if arguments.video_config
            else None
        )
    except (OSError, TimeoutError) as error:
        print(f"Could not read MIPI TX state: {error}", file=sys.stderr)
        return 3

    print(f"MIPI TX selected port (0x19): 0x{tx_select:02x}")
    print(f"MIPI TX state (0x22): 0x{tx_state:02x}")
    print(f"  stable: {'yes' if tx_state & 1 else 'no'}")
    print(f"MIPI TX interrupt status (0x23): 0x{tx_interrupt:02x}")
    print(f"  jitter: {'set' if tx_interrupt & 1 else 'clear'}")
    print(f"  stable-transition: {'set' if tx_interrupt & 2 else 'clear'}")
    print(f"  port interrupts: 0x{(tx_interrupt >> 2) & 0x0f:x}")
    print(f"DSI command packet status (0xc0:0x74): 0x{command_status:02x}")
    print(f"  command FIFO: {'empty' if command_status & 1 else 'not empty'}")
    print(f"  command FIFO full: {'yes' if command_status & 2 else 'no'}")
    print(f"  payload write FIFO full: {'yes' if command_status & 8 else 'no'}")
    print(f"  generic read busy: {'yes' if command_status & 0x40 else 'no'}")
    print(f"DSI PHY status (0xc0:0xb0): 0x{phy_status:02x}")
    print(f"  PLL locked: {'yes' if phy_status & 1 else 'no'}")
    print(f"  clock lane stop state: {'yes' if phy_status & 4 else 'no'}")
    print(f"  data lane 0 stop state: {'yes' if phy_status & 0x10 else 'no'}")
    print(f"  data lane 1 stop state: {'yes' if phy_status & 0x80 else 'no'}")
    if interrupt_status0 is not None and interrupt_status1 is not None:
        print(f"DSI interrupt status 0 (0xc0:0xbc): 0x{interrupt_status0:08x}")
        print(f"  ACK/PHY error bits: 0x{interrupt_status0:08x} (read acknowledges this latch)")
        print(f"DSI interrupt status 1 (0xc0:0xc0): 0x{interrupt_status1:08x}")
        print(f"  timeout/data error bits: 0x{interrupt_status1:08x} (read acknowledges this latch)")
    if video_config is not None:
        print("DSI video configuration:")
        print(f"  DPI color coding (0xc0:0x10): 0x{video_config['dpi_color_coding']:08x}")
        print(f"  DPI polarity (0xc0:0x14): 0x{video_config['dpi_cfg_pol']:08x}")
        print(f"  DPI LP command timing (0xc0:0x18): 0x{video_config['dpi_lp_cmd_tim']:08x}")
        print(f"  packet handling (0xc0:0x2c): 0x{video_config['pckhdl_cfg']:08x}")
        print(f"  mode (0xc0:0x34): 0x{video_config['mode_cfg']:08x}")
        print(f"  video mode (0xc0:0x38): 0x{video_config['vid_mode_cfg']:08x}")
        print(f"  video packet size (0xc0:0x3c): 0x{video_config['vid_pkt_size']:08x}")
        print(f"  command mode (0xc0:0x68): 0x{video_config['cmd_mode_cfg']:08x}")
        print(f"  timeout counters (0xc0:0x78): 0x{video_config['to_cnt_cfg']:08x}")
        print(f"  BTA timeout (0xc0:0x8c): 0x{video_config['bta_to_cnt']:08x}")
        print(f"  LP clock control (0xc0:0x94): 0x{video_config['lpclk_ctrl']:08x}")
        print(
            f"  horizontal timing hsa/hbp/hline (0xc0:0x48/0x4c/0x50): "
            f"{video_config['vid_hsa_time']}/{video_config['vid_hbp_time']}/"
            f"{video_config['vid_hline_time']}"
        )
        print(
            f"  vertical timing vsa/vbp/vfp/vactive (0xc0:0x54/0x58/0x5c/0x60): "
            f"{video_config['vid_vsa_lines']}/{video_config['vid_vbp_lines']}/"
            f"{video_config['vid_vfp_lines']}/{video_config['vid_vactive_lines']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())