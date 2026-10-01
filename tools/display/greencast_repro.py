#!/usr/bin/env python3
"""Reproduce the DeckSight "wrong colour on initialisation" (green cast) and log
the eDP link state around every occurrence.

The known-issues page says the cast appears on about a third of display
initialisations: wake from sleep, Game UI <-> Desktop, refresh-rate changes and
game launches. This script fires those initialisations on demand, snapshots the
eDP sink's DPCD registers (the ANX bridge's eDP side) before and after, collects
new kernel DRM messages, and asks you to label what the panel shows.

Comparing good and green snapshots tells us which side of the ANX bridge fails:
  * link rate/lanes/lock/alignment/error counters differ -> eDP side (APU <-> ANX)
  * eDP link identical and healthy                       -> MIPI side (ANX <-> OLED)

The green cast is only visible on the panel (screenshots are unaffected), so
labelling is manual.

Only reads DPCD over AUX (/dev/drm_dp_aux*); nothing is written to the sink.
Must run as root on the host, from a terminal you can still see or reach over SSH:

  sudo ./greencast_repro.py snapshot --label green      # record a natural occurrence
  sudo ./greencast_repro.py run --trigger dpms -n 30    # provoke and log
  sudo ./greencast_repro.py summary LOGFILE
"""

import argparse
import datetime
import glob
import json
import os
import pwd
import random
import re
import subprocess
import sys
import time

CONNECTOR = "card0-eDP-1"
OUTPUT = "eDP-1"
NATIVE_MODE = "1080x1920@60"
ALT_MODE = "1024x768@60"

LABELS = {"": "ok", "g": "green", "c": "other-colour", "s": "static/garbage",
          "b": "black", "f": "flicker"}

# DPCD ranges worth keeping raw (address, length)
RAW_RANGES = [(0x000, 16), (0x070, 2), (0x100, 16), (0x200, 16), (0x210, 8),
              (0x600, 1), (0x700, 2), (0x2008, 3)]


# ---------------------------------------------------------------------------
# DPCD
# ---------------------------------------------------------------------------

def aux_device():
    devs = glob.glob(f"/sys/class/drm/{CONNECTOR}/drm_dp_aux*")
    if not devs:
        sys.exit(f"no drm_dp_aux device under /sys/class/drm/{CONNECTOR}")
    return "/dev/" + os.path.basename(devs[0])


def read_dpcd(dev):
    raw = {}
    fd = os.open(dev, os.O_RDONLY)
    try:
        for addr, length in RAW_RANGES:
            try:
                raw[addr] = os.pread(fd, length, addr)
            except OSError as e:
                raw[addr] = e.strerror
    finally:
        os.close(fd)
    return raw


def decode(raw):
    def b(addr):
        for base, data in raw.items():
            if isinstance(data, bytes) and base <= addr < base + len(data):
                return data[addr - base]
        return None

    out = {}
    if b(0x001) is not None:
        out["max_link_rate_gbps"] = round(b(0x001) * 0.27, 2)
        out["max_lanes"] = b(0x002) & 0x1f
    if b(0x100) is not None:
        out["link_rate_gbps"] = round(b(0x100) * 0.27, 2)
        lanes = b(0x101) & 0x1f
        out["lanes"] = lanes
        out["enhanced_framing"] = bool(b(0x101) & 0x80)
        out["training_pattern"] = b(0x102) & 0x0f
        out["drive_preemph"] = [b(0x103 + i) for i in range(lanes)]
        out["downspread"] = bool((b(0x107) or 0) & 0x10)
    if b(0x202) is not None:
        lanes = out.get("lanes", 4) or 4
        status = [(b(0x202 + i // 2) >> (4 * (i % 2))) & 0x7 for i in range(lanes)]
        out["lane_status"] = status          # 7 = CR done | EQ done | symbol locked
        out["interlane_aligned"] = bool(b(0x204) & 0x01)
        out["link_ok"] = all(s == 7 for s in status) and out["interlane_aligned"]
        out["sink_count"] = b(0x200) & 0x3f
        out["irq_vector"] = b(0x201)
        out["sink_status"] = b(0x205)
    if b(0x210) is not None:
        errs = []
        for i in range(out.get("lanes", 4) or 4):
            lo, hi = b(0x210 + 2 * i), b(0x211 + 2 * i)
            # 0xffff = counter not implemented (the ANX returns this); bit 15 = valid
            errs.append(None if (lo, hi) == (0xff, 0xff) or not hi & 0x80 else (hi & 0x7f) << 8 | lo)
        out["symbol_errors"] = errs          # clear-on-read: errors since last read
    if b(0x600) is not None:
        out["sink_power"] = {1: "D0", 2: "D3", 5: "D3-aux"}.get(b(0x600), hex(b(0x600)))
    if b(0x070) is not None:
        out["psr_version"] = b(0x070)
    if b(0x2008) is not None:
        out["psr_sink_state"] = b(0x2008) & 0x07
    return out


# amdgpu debugfs files describing what the APU is sending (colour format, bpc, link)
DEBUGFS_FILES = ["link_settings", "output_bpc", "psr_state", "amdgpu_current_bpc",
                 "amdgpu_current_colorspace", "replay_state", "ilr_setting"]
STATE_KEYS = re.compile(r"^\s*(mode:|colorspace|max_requested_bpc|content_type|"
                        r"HDR_OUTPUT_METADATA|hdr_output_metadata|enable|active|self_refresh_active)")


DEBUGFS_ROOT = "/sys/kernel/debug/dri"


def read_debugfs():
    """Read-only view of the amdgpu side. Needs root and a mounted debugfs."""
    out = {}
    for d in glob.glob(f"{DEBUGFS_ROOT}/*/{OUTPUT}"):
        for name in DEBUGFS_FILES:
            try:
                with open(os.path.join(d, name)) as f:
                    out[name] = " | ".join(l.strip() for l in f.read().splitlines() if l.strip())
            except OSError:
                pass
        for c in glob.glob(os.path.join(os.path.dirname(d), "crtc-*")):
            for name in ("amdgpu_current_bpc", "amdgpu_current_colorspace"):
                try:
                    with open(os.path.join(c, name)) as f:
                        out[f"{os.path.basename(c)}/{name}"] = f.read().strip()
                except OSError:
                    pass
        # connector + active CRTC lines from the atomic state dump
        try:
            with open(os.path.join(os.path.dirname(d), "state")) as f:
                blocks = f.read().split("\n")
        except OSError:
            break
        keep, section = [], None
        for line in blocks:
            if not line.startswith((" ", "\t")):
                section = line.strip()
            if section and (OUTPUT in section or section.startswith("crtc")) and \
                    (STATE_KEYS.match(line) or not line.startswith((" ", "\t"))):
                keep.append(line.strip())
        out["state"] = keep
        break
    return out


def snapshot(dev):
    raw = read_dpcd(dev)
    return {"decoded": decode(raw),
            "raw": {f"{a:#06x}": (d.hex() if isinstance(d, bytes) else f"ERR {d}")
                    for a, d in raw.items()},
            "amdgpu": read_debugfs()}


# ---------------------------------------------------------------------------
# Kernel messages
# ---------------------------------------------------------------------------

class KernelLog:
    KEYWORDS = ("amdgpu", "drm", "dp", "edp", "link", "hpd", "psr", "dmub")

    def __init__(self):
        self.mark = self._lines()

    def _lines(self):
        res = subprocess.run(["dmesg"], capture_output=True, text=True)
        return res.stdout.splitlines()

    def new(self):
        lines = self._lines()
        fresh = lines[len(self.mark):] if len(lines) >= len(self.mark) else lines
        self.mark = lines
        return [l for l in fresh if any(k in l.lower() for k in self.KEYWORDS)]


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------

class Session:
    """Run desktop-session commands as the logged-in user."""

    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.user = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
        self.uid = pwd.getpwnam(self.user).pw_uid
        run = f"/run/user/{self.uid}"
        wl = sorted(glob.glob(f"{run}/wayland-[0-9]"))
        self.env = {
            "XDG_RUNTIME_DIR": run,
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run}/bus",
            "WAYLAND_DISPLAY": os.path.basename(wl[0]) if wl else "wayland-0",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        }
        xauth = sorted(glob.glob(f"{run}/xauth_*"))
        if xauth:
            self.env["XAUTHORITY"] = xauth[0]
        # Over SSH there is no DISPLAY; borrow the one Steam uses (Game Mode's Xwayland)
        self.env.update(self._steam_x_env())

    def _steam_x_env(self):
        for comm in glob.glob("/proc/[0-9]*/comm"):
            try:
                with open(comm) as f:
                    if f.read().strip() != "steam":
                        continue
                if os.stat(os.path.dirname(comm)).st_uid != self.uid:
                    continue
                with open(os.path.join(os.path.dirname(comm), "environ"), "rb") as f:
                    env = dict(kv.split(b"=", 1) for kv in f.read().split(b"\0") if b"=" in kv)
            except OSError:
                continue
            return {k: env[k.encode()].decode() for k in ("DISPLAY", "XAUTHORITY") if k.encode() in env}
        return {}

    def _wrap(self, cmd, as_user):
        full = list(cmd)
        if as_user and os.getuid() == 0:
            full = ["runuser", "-u", self.user, "--", "env"] + \
                   [f"{k}={v}" for k, v in self.env.items()] + full
        return full

    def query(self, *cmd):
        """Run a read-only command even in --dry-run; return stdout without colour codes."""
        try:
            res = subprocess.run(self._wrap(cmd, True), capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return re.sub(r"\x1b\[[0-9;]*m", "", res.stdout)

    def run(self, *cmd, as_user=True):
        full = self._wrap(cmd, as_user)
        if self.dry_run:
            print("   [dry-run]", " ".join(cmd))
            return
        res = subprocess.run(full, capture_output=True, text=True)
        if res.returncode != 0:
            print(f"   warning: {' '.join(cmd)} -> rc {res.returncode}: {res.stderr.strip()[:200]}")


def other_outputs(s):
    """kscreen output names other than the DeckSight panel, e.g. an external DP-1."""
    res = s.query("kscreen-doctor", "-o")
    names = []
    for line in res.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].endswith("Output:") and parts[2] != OUTPUT:
            names.append(parts[2])
    return names


def trig_dpms(s, a):
    # kscreen-doctor --dpms applies to every screen unless excluded
    excl = []
    for name in other_outputs(s):
        excl += ["--dpms-excluded", name]
    s.run("kscreen-doctor", "--dpms", "off", *excl)
    time.sleep(a.off_time)
    s.run("kscreen-doctor", "--dpms", "on", *excl)


def trig_modeset(s, a):
    s.run("kscreen-doctor", f"output.{OUTPUT}.mode.{ALT_MODE}")
    time.sleep(a.off_time)
    s.run("kscreen-doctor", f"output.{OUTPUT}.mode.{NATIVE_MODE}")


def trig_hdr(s, a):
    s.run("kscreen-doctor", f"output.{OUTPUT}.hdr.enable")
    time.sleep(a.off_time)
    s.run("kscreen-doctor", f"output.{OUTPUT}.hdr.disable")


def trig_suspend(s, a):
    s.run("rtcwake", "-m", "mem", "-s", str(a.sleep_time), as_user=False)


def gamescope_refresh(s):
    """Refresh rate gamescope reports it is driving (root property)."""
    out = s.query("xprop", "-root", "GAMESCOPE_DISPLAY_REFRESH_RATE_FEEDBACK")
    m = re.search(r"=\s*(\d+)", out)
    return int(m.group(1)) if m else None


def trig_refresh(s, a):
    """Game Mode only: set the root-window property Steam uses to request a
    refresh rate. gamescope's DeckSight.lua modegen then does a full modeset
    with a new pixel clock, the path users report the cast on."""
    current = gamescope_refresh(s)
    choices = [r for r in a.rates if r != current] or a.rates
    rate = random.choice(choices)
    a.last_rate = rate
    print(f"   refresh {current} -> {rate} Hz")
    s.run("xprop", "-root", "-f", "GAMESCOPE_DYNAMIC_REFRESH", "32c",
          "-set", "GAMESCOPE_DYNAMIC_REFRESH", str(rate))


def check_refresh(s, a):
    got = gamescope_refresh(s)
    if got != a.last_rate:
        print(f"   warning: gamescope reports {got} Hz, requested {a.last_rate} Hz "
              "(is a game running? Steam may apply dynamic refresh only in-game)")
    return got


TRIGGERS = {"dpms": trig_dpms, "modeset": trig_modeset, "hdr": trig_hdr,

            "suspend": trig_suspend, "refresh": trig_refresh}
DESKTOP_MIX = ["dpms", "modeset", "suspend"]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def ask_label(auto):
    if auto:
        return "unlabelled"
    keys = "  ".join(f"[{k or 'Enter'}]={v}" for k, v in LABELS.items())
    while True:
        ans = input(f"   Panel now? {keys}  [q]=quit: ").strip().lower()
        if ans == "q":
            return None
        if ans in LABELS:
            return LABELS[ans]


def open_log(path):
    if path is None:
        home = pwd.getpwnam(os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name).pw_dir
        d = os.path.join(home, "decksight-greencast")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + ".jsonl")
    f = open(path, "a")
    if os.environ.get("SUDO_UID"):
        os.chown(path, int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
        os.chown(os.path.dirname(path), int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"]))
    return path, f


def brief(dec):
    if "link_rate_gbps" not in dec:
        return "DPCD unreadable"
    return (f"{dec['lanes']}x{dec['link_rate_gbps']}G (max {dec.get('max_lanes')}x"
            f"{dec.get('max_link_rate_gbps')}G) lanes={dec.get('lane_status')} "
            f"aligned={dec.get('interlane_aligned')} errs={dec.get('symbol_errors')} "
            f"pwr={dec.get('sink_power')} psr={dec.get('psr_sink_state')}")


def brief_amdgpu(amd):
    if not amd:
        return "amdgpu debugfs: unavailable (mount debugfs?)"
    keys = ("output_bpc", "amdgpu_current_bpc", "amdgpu_current_colorspace", "psr_state")
    parts = [f"{k}={v}" for k, v in amd.items() if k.split("/")[-1] in keys]
    parts += [l for l in amd.get("state", []) if l.startswith(("colorspace", "max_requested_bpc"))]
    return "amdgpu: " + "  ".join(parts)


def cmd_snapshot(a):
    dev = aux_device()
    path, log = open_log(a.log)
    snap = snapshot(dev)
    label = a.label or ask_label(False) or "unlabelled"
    log.write(json.dumps({"time": time.time(), "kind": "snapshot", "label": label,
                          "note": a.note, "after": snap}) + "\n")
    print(f"{label}: {brief(snap['decoded'])}\n{brief_amdgpu(snap.get('amdgpu'))}\nlogged to {path}")


def cmd_run(a):
    if os.getuid() != 0 and not a.dry_run:
        sys.exit("run as root (sudo): DPCD reads and rtcwake need it")
    dev = None if a.dry_run else aux_device()
    session = Session(a.dry_run)
    path, log = (None, None) if a.dry_run else open_log(a.log)
    klog = None if a.dry_run else KernelLog()
    names = DESKTOP_MIX if a.trigger == "mix" else [a.trigger]
    print(f"trigger={a.trigger} iterations={a.n} settle={a.settle}s log={path}")
    counts = {}
    for i in range(1, a.n + 1):
        name = random.choice(names)
        print(f"[{i}/{a.n}] {name}")
        before = None if a.dry_run else snapshot(dev)
        t0 = time.time()
        TRIGGERS[name](session, a)
        if a.dry_run:
            continue
        time.sleep(a.settle)
        extra = {}
        if name == "refresh":
            extra = {"requested_hz": a.last_rate, "applied_hz": check_refresh(session, a)}

        after = snapshot(dev)
        kmsgs = klog.new()
        print("   " + brief(after["decoded"]))
        print("   " + brief_amdgpu(after.get("amdgpu")))
        for m in kmsgs[-5:]:
            print("   kmsg:", m[:160])
        label = ask_label(a.auto)
        if label is None:
            break
        counts[label] = counts.get(label, 0) + 1
        log.write(json.dumps({"time": t0, "kind": "trigger", "trigger": name, "iteration": i,
                              **extra, "label": label, "before": before, "after": after,
                              "kernel": kmsgs}) + "\n")
        log.flush()
        if label not in ("ok", "unlabelled") and a.stop_on_bad:
            print("   stopping on first bad result (--stop-on-bad); panel left as is for inspection")
            break
    if log:
        print(f"results: {counts}\nlogged to {path}\nsummary: {sys.argv[0]} summary {path}")


def rate_breakdown(rows):
    """For refresh-rate runs: bad results per target rate band and per transition."""
    rr = [r for r in rows if r.get("requested_hz")]
    if not rr:
        return
    bad = lambda r: r["label"] not in ("ok", "timeout", "unlabelled")
    print(f"\n== by target refresh rate ({len(rr)} modesets, {sum(map(bad, rr))} bad)")
    for lo in range(40, 90, 10):
        band = [r for r in rr if lo <= r["requested_hz"] < lo + 10]
        if band:
            b = sum(map(bad, band))
            print(f"   {lo}-{lo + 9} Hz: {b}/{len(band)} bad  "
                  + " ".join(f"{r['requested_hz']}{'*' if bad(r) else ''}" for r in band))
    # The bad state is sticky across modesets, so per-rate counts mostly measure how
    # long it persisted. What matters is how often a modeset enters / leaves it.
    seq = [bad(r) for r in rr]
    from_ok = [b for a_, b in zip(seq, seq[1:]) if not a_]
    from_bad = [b for a_, b in zip(seq, seq[1:]) if a_]
    print(f"   modesets from a good picture that broke it: {sum(from_ok)}/{len(from_ok)}")
    print(f"   modesets from a bad picture that cleared it: {sum(1 for b in from_bad if not b)}/{len(from_bad)}")
    print("   sequence: " + " ".join(f"{r['requested_hz']}{'' if not bad(r) else ('C' if 'colour' in r['label'] or r['label'] == 'green' else 'S')}"
                                     for r in rr) + "   (C = colour cast, S = static/other)")
    low_bpc = sorted({r["requested_hz"] for r in rr
                      if "Current: 6" in (r["after"].get("amdgpu") or {}).get("crtc-0/amdgpu_current_bpc", "")})
    if low_bpc:
        print(f"   amdgpu fell back to 6 bpc (link bandwidth) at: {low_bpc} Hz")
    ups = [r for r in rr if r.get("previous_hz") and r["requested_hz"] > r["previous_hz"]]
    downs = [r for r in rr if r.get("previous_hz") and r["requested_hz"] < r["previous_hz"]]
    for name, grp in (("rate increases", ups), ("rate decreases", downs)):
        if grp:
            print(f"   {name}: {sum(map(bad, grp))}/{len(grp)} bad")
    print("   bad transitions: " + ", ".join(f"{r.get('previous_hz')}->{r['requested_hz']} ({r['label']})"
                                           for r in rr if bad(r)))


def cmd_summary(a):
    if not a.logfile:
        user = os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
        logs = sorted(glob.glob(os.path.join(pwd.getpwnam(user).pw_dir, "decksight-greencast", "*.jsonl")))
        if not logs:
            sys.exit("no logs in ~/decksight-greencast")
        a.logfile = logs[-1]
    print(f"log: {a.logfile}")
    rows = [json.loads(l) for l in open(a.logfile) if l.strip()]
    for r in rows:  # re-decode from raw bytes so decoder fixes apply to old logs
        raw = r["after"].get("raw")
        if raw and not any(v.startswith("ERR") for v in raw.values()):
            r["after"]["decoded"] = decode({int(k, 16): bytes.fromhex(v) for k, v in raw.items()})
    by = {}
    for r in rows:
        by.setdefault(r["label"], []).append(r)
    for label, rs in by.items():
        trig = {}
        for r in rs:
            trig[r.get("trigger", "snapshot")] = trig.get(r.get("trigger", "snapshot"), 0) + 1
        print(f"\n== {label}: {len(rs)}  {trig}")
        # Distinct decoded link states for this label
        states = {}
        for r in rs:
            d = dict(r["after"]["decoded"])
            d.pop("symbol_errors", None)
            d.pop("irq_vector", None)
            key = json.dumps(d, sort_keys=True)
            states[key] = states.get(key, 0) + 1
        for k, n in sorted(states.items(), key=lambda x: -x[1]):
            print(f"   x{n}: {k}")
        errs = [r["after"]["decoded"].get("symbol_errors") for r in rs]
        errs = [e for e in errs if e and any(x for x in e if x)]
        print(f"   snapshots with symbol errors: {len(errs)}")
        amd = {}
        for r in rs:
            a = {k: v for k, v in (r["after"].get("amdgpu") or {}).items() if k != "state"}
            key = json.dumps(a, sort_keys=True)
            amd[key] = amd.get(key, 0) + 1
        for k2, n in sorted(amd.items(), key=lambda x: -x[1]):
            print(f"   amdgpu x{n}: {k2}")
        k = sum(1 for r in rs if r.get("kernel"))
        print(f"   with new kernel DRM messages: {k}")
    rate_breakdown(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="record the current link state once (e.g. while green)")
    s.add_argument("--label", choices=list(LABELS.values()))
    s.add_argument("--note", default="")
    s.add_argument("--log")
    s.set_defaults(func=cmd_snapshot)

    r = sub.add_parser("run", help="fire display initialisations and log each one")
    r.add_argument("--trigger", choices=list(TRIGGERS) + ["mix"], default="dpms",
                   help="dpms/modeset/hdr/suspend (Desktop), refresh (Game Mode); for Desktop-mode "
                        "refresh-rate changes use drm_modecycle.py. "
                        "mix = random dpms/modeset/suspend")
    r.add_argument("-n", type=int, default=20, help="iterations")
    r.add_argument("--settle", type=float, default=3.0, help="seconds to wait before snapshot")
    r.add_argument("--off-time", type=float, default=2.0, help="seconds off/in alternate mode")
    r.add_argument("--sleep-time", type=int, default=8, help="rtcwake suspend seconds")
    r.add_argument("--rates", type=int, nargs="+", default=None,
                   help="refresh rates for --trigger refresh (default 40 45 50 55 60)")
    r.add_argument("--auto", action="store_true", help="don't prompt; log unlabelled")
    r.add_argument("--stop-on-bad", action="store_true", help="stop at the first non-ok label")
    r.add_argument("--dry-run", action="store_true", help="print trigger commands only")
    r.add_argument("--log")
    r.set_defaults(func=cmd_run)

    m = sub.add_parser("summary", help="group a log by label and compare link states")
    m.add_argument("logfile", nargs="?", help="default: the newest log in ~/decksight-greencast")
    m.set_defaults(func=cmd_summary)

    a = ap.parse_args()
    if getattr(a, "cmd", None) == "run":
        a.rates = a.rates or [40, 45, 50, 55, 60]
    a.func(a)


if __name__ == "__main__":
    main()
