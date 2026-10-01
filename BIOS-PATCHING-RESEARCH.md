# BIOS Patching Research

## DeckHD Toolchain

Public repository: [DeckHD/BiosMaker](https://github.com/DeckHD/BiosMaker)

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

## Software-Only Color Initialization Investigation

This section records mitigations and diagnostic tests that do not modify BIOS
firmware. They may reduce the symptom or reinitialize the display path, but no
software-only fix for the underlying initialization fault has been confirmed.

### Reported Symptom and Reset Workaround

The [DeckSight known-issues page](https://www.shadetechnik.com/decksight-known-issues)
describes a wrong-color state during display initialization, reported to happen
about one-third of the time and less often after a June 2025 improvement. It can
occur after waking from sleep or changing display modes, including Game UI to
Desktop. The documented workaround is a quick sleep/wake using the power button;
a full shutdown is not required.

The ACPI investigation above is consistent with this workaround: an actual
suspend/resume restores the panel, while manually calling `DPCY()`, `_WAK(3)`,
and re-enabling eDP does not. The real resume path runs additional firmware
initialization. A software-triggered DPMS off/on is worth comparing, but should
not be assumed to perform the same EC/ANX sequence as suspend/resume.

### Software Variables to Test

**Refresh rate and timings.** [DeckSight.lua](Gamescope/DeckSight.lua) generates
1080x1920 modes from 40 to 80 Hz with custom porch timings. It notes that some
refresh rates had less stable initialization and that a longer vertical sync may
improve synchronization. A fixed 60 Hz test can show whether dynamic modesets
contribute. [drm_modecycle.py](tools/display/drm_modecycle.py) exercises custom
timings with color bars and captures link state. It holds DRM master and blocks
suspend because suspending while it owns DRM master hung the Deck during testing;
do not combine its run with suspend tests.

**HDR and colorimetry.** The Gamescope display entry currently sets
`hdr.force_enabled = true` and uses measured colorimetry; specification values
are also defined in the Lua file. Compare HDR disabled, measured versus
specification colorimetry, and fixed refresh as separate A/B tests. Gamescope
settings affect Game Mode, not the Plasma Desktop pipeline, so they cannot explain
a cast that also occurs independently in Desktop.

**EDID.** [The EDID README](edid/README.md) documents kernel and Gamescope
overrides that can test mode lists, HDR metadata, and colorimetry without
reflashing. EDID changes do not program the EC's ANX MIPI/OCM initialization, so
treat this as a metadata/modeset test rather than a bridge reset.

**BPC, colorspace, PSR, and Panel Replay.**
[greencast_repro.py](tools/display/greencast_repro.py) records AMDGPU output
BPC/colorspace, PSR/Replay state, DPCD link status, and kernel DRM messages around
initialization triggers. Test these for correlation with the cast before
changing driver settings; do not assume PSR or link training is the cause based
on a single sample.

**ICC or GPU LUT correction.** DRM/KMS exposes CRTC degamma LUT, CTM, and gamma
LUT properties when supported by the driver ([kernel KMS color management](https://docs.kernel.org/gpu/drm-kms.html#color-management-properties)).
They can correct a stable, measurable color bias. They are not a reliable fix
for an intermittent bridge/panel initialization state, especially if the
corruption occurs after the GPU's color pipeline. The repository's ICC profile
is for SDR color correction; it does not replay display initialization and is
not a Game Mode fix by itself.

### Capture and Interpretation

Use the existing scripts to capture both a known-good and a bad initialization
before resetting the panel:

**Capture both states.** Record the visible result and capture
`greencast_repro.py snapshot` for good and bad initializations before resetting
the panel. Keep the same static test image and, when using a camera, lock exposure
and white balance.

**Compare state.** Compare the DPCD link and AMDGPU BPC/colorspace/PSR/Replay
fields. Use `panel_status.py crc` with a documented CRC source where possible;
CRC source placement is driver-specific, so record the selected source rather
than assuming `auto` is a physical-panel measurement.

**Interpret cautiously.** If the panel looks green while the relevant upstream
CRC is unchanged and the eDP link state is healthy, that points downstream of
the CRC tap, toward the Analogix-to-panel path. It is evidence, not proof, of an
MIPI/panel-side issue. If CRC, BPC, colorspace, HDR state, or link status changes
with the cast, prioritize that software/modeset path.

**Isolate triggers.** Compare DPMS cycling with actual suspend/resume, then test
fixed 60 Hz and HDR disabled one at a time. Avoid randomized combined tests when
attributing causality; a bad color state may persist across later modesets.

### Current Software-Only Assessment

The most credible no-BIOS workaround is a user-triggered real suspend/resume,
which matches the vendor's reported recovery and the observed ACPI behavior. A
hotkey that requests suspend can make this easier, but software currently has no
reliable way to detect the visible green cast automatically. Fixed refresh/HDR
settings may reduce triggering if a mode transition is implicated. A custom
GPU LUT is only appropriate if captures establish a stable upstream color error.

Replaying the bridge sequence through the exposed ACPI `ANW1`/`ANW4` methods is
a possible research direction, not an established fix. The required sequence
has not been recovered or validated, and `RDDI()` has not proven reliable as a
general register-dump interface. Do not send guessed bridge writes.

## Sources

- [DeckHD BiosMaker](https://github.com/DeckHD/BiosMaker)
- [DeckSight known issues](https://www.shadetechnik.com/decksight-known-issues)
- [Linux DRM/KMS color management](https://docs.kernel.org/gpu/drm-kms.html#color-management-properties)
- [DeckSight discussion 13](https://github.com/ShadeTechnik/DeckSight-Public/discussions/13)
- [DeckSight discussion 34](https://github.com/ShadeTechnik/DeckSight-Public/discussions/34)
- [DeckSight discussion 26](https://github.com/ShadeTechnik/DeckSight-Public/discussions/26)
- [DeckSight discussion 60](https://github.com/ShadeTechnik/DeckSight-Public/discussions/60)
