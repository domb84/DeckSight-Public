# EDID Files contained here.

decksight_edid.bin is the EDID for the DeckSight panel, including the CEA-861 extension block (HDR static metadata + BT2020 colorimetry). It's patched into two locations in the DeckSight BIOS: the OS-facing EDID location gets the full extended EDID (base + CEA extension), which is what the kernel exposes for the eDP panel at boot - this is what Gamescope and the desktop session actually read. The early-boot panel table only holds the 128-byte base block (no CEA extension, no HDR/colorimetry data) and is used solely for early VBIOS/GOP panel init, so it's patched with just the base block's bytes for consistency. It can also be used by kernel parameter loading or parsed into Gamescope for testing without reflashing.

## Usage
### Kernel override (Not recommended, mainly for testing without reflashing)
Place decksight_edid.bin in /lib/firmware/edid/decksight_edid.bin

Add drm.edid_firmware=eDP-1:edid/decksight_edid.bin to end of GRUB_CMDLINE_LINUX_DEFAULT= in /etc/default/grub

sudo update-grub
sudo mkinitcpio -P

### For Gamescope (mainly for testing without reflashing)
Create directory and place decksight_edid.bin in ~/.local/share/decksight/

Create directory and place decksight-edid.conf in ~/.config/environment.d/ - This file creates an environment variable which redirects gamescope to parse this extended EDID

$ systemctl --user daemon-reexec

## Notes
Since the full decksight_edid.bin is patched directly into the BIOS's OS-facing EDID location, the kernel exposes the full extended EDID (including the CEA extension) for the eDP panel at boot, system-wide. Both Gamescope and the desktop session (KWin/Plasma) read this same EDID, so neither runtime override above is required on a BIOS that already has it patched in - they're useful for testing EDID changes before committing to a reflash. The early-boot panel table never carries the CEA extension, only the base timing - it doesn't affect HDR or refresh-rate capability since nothing reads it after early boot.
