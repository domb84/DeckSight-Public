#!/usr/bin/env python3
"""Read-only checks for whether the display is currently in a bad state.

Two independent, standard kernel debugfs mechanisms, neither of which touches
/dev/mem, firmware, or any hardware register directly:

  status  - dump every read-only eDP-1 debugfs attribute (link, PHY, PSR,
            colour, SDP metadata, DSC, backlight). Pure reads.

  crc     - use DRM's CRC capture (the same debugfs interface IGT/kernel CI
            uses to catch corrupted frames): turn it on, watch it for a bit,
            turn it off. Confirms whether the GPU's own output is stable and
            matches what it should be for a static picture. Does ONE
            documented write: "echo auto > crc/control", reverted with
            "echo none" even on Ctrl+C or an error. It cannot fix, corrupt,
            or set anything on the panel; it only counts frames.

  watch   - repeat `status` every few seconds so a change (e.g. the moment a
            cast appears, or clears after sleep/wake) shows up in the diff.

Usage (read-only 'status'/'watch' work as any user; 'crc' needs root for the
debugfs write):
  ./panel_status.py status
  ./panel_status.py watch --interval 2
  sudo ./panel_status.py crc --duration 3
"""

import argparse
import glob
import os
import re
import sys
import time

DEBUGFS_ROOT = "/sys/kernel/debug/dri"
OUTPUT = "eDP-1"

# Every file here is read-only from userspace (the kernel exposes them purely
# for inspection; some, like test_pattern, also accept writes elsewhere in
# the driver, but we never write to any of them).
STATUS_FILES = [
    "link_settings", "phy_settings", "lttpr_status", "ilr_setting",
    "output_bpc", "max_bpc", "vrr_range",
    "psr_capability", "psr_state", "psr_residency", "disallow_edp_enter_psr",
    "replay_capability", "replay_state", "replay_residency",
    "dsc_clock_en", "dsc_slice_width", "dsc_slice_height", "dsc_slice_bpg",
    "dsc_bits_per_pixel", "dsc_pic_width", "dsc_pic_height",
    "amdgpu_current_backlight_pwm", "amdgpu_target_backlight_pwm",
    "hdcp_sink_capability",
]

# Confirmed write-only on this kernel (read -> EINVAL): they accept data to
# inject (a forced test pattern, an SDP packet to send) but report nothing
# back, so there is nothing for a read-only tool to show.
WRITE_ONLY_FILES = ["sdp_message", "test_pattern"]


def edp_dir():
    dirs = sorted(glob.glob(f"{DEBUGFS_ROOT}/*/{OUTPUT}"))
    # Prefer the PCI-address-named instance; the numeric aliases point at the same files.
    pci = [d for d in dirs if re.search(r"[0-9a-f]{4}:", d)]
    if not (pci or dirs):
        sys.exit(f"no {OUTPUT} debugfs directory (run as root; is debugfs mounted?)")
    return (pci or dirs)[0]


def read_status(d):
    out = {}
    for name in STATUS_FILES:
        try:
            with open(os.path.join(d, name)) as f:
                out[name] = f.read().strip()
        except OSError as e:
            out[name] = f"<{e.strerror or e}>"
    for name in WRITE_ONLY_FILES:
        out[name] = "<write-only, not readable>"
    return out


def print_status(status, prefix="  "):
    for name in STATUS_FILES:
        val = status.get(name, "")
        if "\n" in val:
            print(f"{prefix}{name}:")
            for line in val.splitlines():
                print(f"{prefix}    {line}")
        else:
            print(f"{prefix}{name}: {val}")


def cmd_status(a):
    d = edp_dir()
    print(f"reading {d}\n")
    print_status(read_status(d))


def cmd_watch(a):
    d = edp_dir()
    prev = None
    print(f"watching {d} every {a.interval}s (Ctrl+C to stop)\n")
    try:
        while True:
            now = read_status(d)
            ts = time.strftime("%H:%M:%S")
            if prev is None:
                print(f"[{ts}] initial:")
                print_status(now)
            else:
                changed = {k: (prev[k], now[k]) for k in now if prev.get(k) != now[k]}
                if changed:
                    print(f"[{ts}] changed:")
                    for k, (old, new) in changed.items():
                        print(f"    {k}: {old!r} -> {new!r}")
                else:
                    print(f"[{ts}] (no change)")
            prev = now
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass


def crc_dir_for_edp():
    """The crtc-N/crc directory currently driving eDP-1, found via the atomic
    state dump (same read-only 'state' file greencast_repro.py already uses)."""
    card_dir = os.path.dirname(edp_dir())
    with open(os.path.join(card_dir, "state")) as f:
        text = f.read()
    section = None
    crtc = None
    for line in text.splitlines():
        if not line.startswith((" ", "\t")):
            section = line.strip()
            if section.startswith(f"connector[") and OUTPUT in section:
                pass
        if section and f"]: {OUTPUT}" in section:
            m = re.match(r"\s*crtc=(crtc-\d+)", line)
            if m:
                crtc = m.group(1)
                break
    if not crtc:
        sys.exit(f"{OUTPUT} has no active CRTC (display off / not scanning out)")
    path = os.path.join(card_dir, crtc, "crc")
    if not os.path.isdir(path):
        sys.exit(f"no crc/ directory under {path}")
    return path, crtc


def _read_control(control):
    try:
        with open(control) as f:
            return f.read().strip()
    except OSError as e:
        return f"<unreadable: {e.strerror or e}>"


def _read_crc_records(data, retries=8, delay=0.15):
    """Low-level read with an explicit large count: the kernel's CRC data file
    rejects reads smaller than one fixed-size record (EINVAL), and there is a
    short window right after enabling before any record exists yet, so a
    couple of early attempts failing is normal, not an error."""
    fd = os.open(data, os.O_RDONLY | os.O_NONBLOCK)
    try:
        lines = []
        for attempt in range(retries):
            try:
                chunk = os.read(fd, 65536)
                lines += [l for l in chunk.decode(errors="replace").splitlines() if l.strip()]
                return lines
            except BlockingIOError:
                pass  # nothing queued yet
            except OSError as e:
                if e.errno != 22 or attempt == retries - 1:  # 22 = EINVAL
                    raise
            time.sleep(delay)
        return lines
    finally:
        os.close(fd)


def cmd_crc(a):
    if os.getuid() != 0:
        sys.exit("run as root: the CRC control file needs it (the only write this tool makes)")
    crc_dir, crtc = crc_dir_for_edp()
    control = os.path.join(crc_dir, "control")
    data = os.path.join(crc_dir, "data")
    print(f"eDP-1 is on {crtc}; capturing CRC for {a.duration}s ({crc_dir}), source={a.source!r}")

    with open(control, "w") as f:
        f.write(a.source)
    print(f"  control now reads: {_read_control(control)!r}")
    try:
        _read_crc_records(data)  # drain/settle; ignore what was already queued
        time.sleep(a.duration)
        lines = _read_crc_records(data, retries=1)
    except OSError as e:
        print(f"could not read CRC data: {e.strerror or e} (errno {e.errno})")
        print(f"  control reads: {_read_control(control)!r} -- if this reverted to something "
              "other than 'auto' on its own, this CRTC/source combination may not be supported "
              "on this kernel; CRC has been turned back off either way.")
        return
    finally:
        with open(control, "w") as f:
            f.write("none")

    if not lines:
        print("no CRC samples captured in the capture window (display may have gone through "
              "a modeset, or nothing is currently being scanned out to eDP-1)")
        return

    crcs = [tuple(l.split()[1:]) for l in lines]
    distinct = sorted(set(crcs))
    print(f"{len(lines)} frames sampled, {len(distinct)} distinct CRC value(s)")
    if len(distinct) == 1:
        print(f"  constant: {distinct[0]}  -> GPU output is stable (matches a static picture)")
    else:
        print("  GPU output is CHANGING frame to frame while nominally static:")
        counts = {}
        for c in crcs:
            counts[c] = counts.get(c, 0) + 1
        for c, n in sorted(counts.items(), key=lambda x: -x[1])[:8]:
            print(f"    {c}: {n}x")
        print("  -> either the source content really is changing, or something upstream "
              "(compositor, gamma/CTM, PSR) is touching the frame each vblank")
    print("\nNote: this only shows what the GPU scanned out. If the panel/bridge shows a "
          "cast while this reads a single stable CRC, the corruption is downstream of the "
          "GPU (bridge output or panel), matching the DPCD/amdgpu-side data already collected.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="dump read-only eDP-1 debugfs state once").set_defaults(func=cmd_status)

    w = sub.add_parser("watch", help="repeat status, print what changed")
    w.add_argument("--interval", type=float, default=2.0)
    w.set_defaults(func=cmd_watch)

    c = sub.add_parser("crc", help="capture DRM CRC for a few seconds (root; one reversible debugfs write)")
    c.add_argument("--duration", type=float, default=3.0)
    c.add_argument("--source", default="auto",
                   help="CRC source to select (see the list `control` reports if this fails, "
                        "e.g. crtc, dprx)")
    c.set_defaults(func=cmd_crc)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
