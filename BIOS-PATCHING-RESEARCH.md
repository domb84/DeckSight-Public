# BIOS Patching Research

## DeckHD Toolchain

Public repository: https://github.com/DeckHD/BiosMaker

The DeckHD BIOS workflow uses:

- `uninsyde`: extracts an Insyde BIOS image into `BIOSIMG.bin`.
- `UEFIReplace`: replaces UEFI modules/resources, such as the splash image.
- `patcher.cpp`: DeckHD-specific binary patcher.
- `chicago_registers.h`: Analogix bridge register definitions.
- `edid.bin`: replacement panel EDID.
- `biosmaker.sh`: orchestration script.

The repository identifies `uninsyde` as coming from `jbit/uninsyde` and
`UEFIReplace` as coming from the LongSoft UEFITool project.

The generated BIOS is not automatically converted into a valid signed `.fd`.
The README says the user must provide a method to generate the final `.fd` or
flash the generated image with external hardware.

## What DeckHD Patches

DeckHD patches the EC firmware blocks embedded in the BIOS, not only the UEFI
display driver. `patcher.cpp` modifies both copies:

```text
0x00000 - 0x1ffff
0x40000 - 0x5ffff
```

It patches or replaces:

- Panel EDID
- MIPI DCS initialization commands
- ANX downstream DPCD configuration
- ANX SPI/OCM timing registers
- MIPI lane count and panel mode
- Horizontal and vertical panel timings
- Panel frame rate
- EC checksum at the end of each EC image

The patcher uses these command structures:

```text
ANX_MIPI_PORT_CMD
ANX_SLAVE_CMD
ANX_SPI_CMD
```

Relevant timing fields include:

```text
SW_H_ACTIVE
SW_HFP
SW_HSYNC
SW_HBP
SW_V_ACTIVE
SW_VFP
SW_VSYNC
SW_VBP
SW_PANEL_FRAME_RATE
SW_PANEL_INFO_0
SW_PANEL_INFO_1
```

This demonstrates that EDID and Gamescope timing changes alone are not the
complete bridge configuration. The ANX bridge has its own MIPI, DPCD, OCM, and
panel timing state.

## DeckSight BIOS Findings

The DeckSight BIOS report contains a DXE module named:

```text
ANX7580OCMflash
```

The BIOS strings also contain:

```text
ANX7530 U
ITE 5570
s3_turn_on_eDP
```

The stock BIOS artifact in this repository contains the same ANX and resume
identifiers at the same offsets as the DeckSight BIOS. The shared
`s3_turn_on_eDP` firmware region is byte-identical between the stock and
DeckSight images examined here.

This indicates that the original Steam Deck firmware already supports the
Analogix bridge. DeckSight changes the downstream panel configuration and EC
payload rather than adding bridge support from scratch.

## ACPI Findings

The live DSDT exposes EC methods under:

```text
\\_SB.PCI0.LPC0.EC0.VFCD
```

The ACPI methods include:

```text
ANR1 / ANR4  bridge reads
ANW1 / ANW4  bridge writes
DPCY          display-cycle command
RDDI          diagnostic read wrapper
```

`DPCY()` acquires the EC mutex and writes `0xCB` to the EC display command
port. It successfully powers the display path off during testing.

The normal sleep/wake path uses:

```text
_PTS: IO6C = 0xCF
_WAK:  IO6C = 0xCE
```

Calling `DPCY()`, then `_WAK(3)`, then re-enabling the eDP connector does not
restore the panel. A real suspend/wake does restore it, proving that the full
firmware resume sequence performs additional ANX initialization.

`RDDI()` is defined but unused elsewhere in the ACPI tables. Attempts to use
it as a general register dump returned zero/status-buffer artifacts, so it
should not be treated as a reliable ANX register inspection API.

## Current Conclusion

The most promising fix surface is the EC/ANX firmware payload, not Lua, EDID,
KWin, or ordinary DRM modesetting. A complete recovery sequence would need to
replay the ANX MIPI/DPCD/OCM/panel initialization that firmware performs during
`s3_turn_on_eDP`.

The DeckHD `patcher.cpp` is the strongest public reference for the required
patching model. It may provide the register structures and command-table format
needed to compare or repair DeckSight's duplicated EC blocks.

## Sources

- DeckHD BiosMaker: https://github.com/DeckHD/BiosMaker
- DeckSight known issues: https://www.shadetechnik.com/decksight-known-issues
- DeckSight discussion 13: https://github.com/ShadeTechnik/DeckSight-Public/discussions/13
- DeckSight discussion 34: https://github.com/ShadeTechnik/DeckSight-Public/discussions/34
- DeckSight discussion 26: https://github.com/ShadeTechnik/DeckSight-Public/discussions/26
- DeckSight discussion 60: https://github.com/ShadeTechnik/DeckSight-Public/discussions/60