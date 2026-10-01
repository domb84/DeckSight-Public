#!/usr/bin/env python3
"""Drive refresh-rate modesets on the DeckSight panel from Desktop mode.

KWin can only use the EDID's single 60 Hz mode, and the debugfs EDID override
does not take effect on this amdgpu eDP panel. So this tool switches to a spare
text console (KWin gives up the display while its VT is inactive), takes DRM
master itself and does what gamescope does in Game Mode: full modesets with
custom 1080x1920 timings (DeckSight.lua blanking) at different refresh rates.

The screen shows colour bars (red, green, blue, white, grey), so a missing
channel is obvious. After each modeset, label the result with a keyboard or
controller; the eDP/amdgpu snapshot is logged in greencast_repro.py's format:

  Enter / A        ok               G          green cast
  C / Up / Y       other colour     S / Down / X   static or garbage
  B                black            F          flicker
  Esc / Q / B-btn  quit

The test console (VT5, its login prompt stopped for the run) shows this legend
and a status line per modeset on any other connected screen, e.g. an external
monitor, and receives the keyboard.

Always returns to the desktop VT on exit, including crashes (a watchdog process
switches back if this one dies).

  sudo ./drm_modecycle.py -n 40
  sudo ./greencast_repro.py summary ~/decksight-greencast/<file>.jsonl
"""

import argparse
import ctypes
import fcntl
import glob
import json
import mmap
import os
import random
import re
import select
import struct
import subprocess
import sys
import termios
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import greencast_repro as repro  # noqa: E402

# Mode timings are taken from the gamescope DeckSight.lua that is actually
# installed, so modesets match Game Mode exactly (see load_lua).
H_ACTIVE, V_ACTIVE = 1080, 1920
H_FP, H_SYNC, H_BP = 48, 32, 80
V_FP, V_SYNC, V_BP = 3, 14, 61
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_lua():
    user = os.environ.get("SUDO_USER") or os.environ.get("USER", "deck")
    installed = os.path.expanduser(f"~{user}/.config/gamescope/scripts/DeckSight.lua")
    return installed if os.path.exists(installed) else os.path.join(REPO, "Gamescope", "DeckSight.lua")


def load_lua(path):
    """Porch timings and refresh list from DeckSight.lua's modegen."""
    global H_FP, H_SYNC, H_BP, V_FP, V_SYNC, V_BP
    src = open(path).read()
    h = re.search(r"set_h_timings\(\s*mode\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", src)
    v = re.search(r"set_v_timings\(\s*mode\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", src)
    r = re.search(r"deckSight_refresh_rates\s*=\s*\{([^}]*)\}", src)
    if not (h and v and r):
        sys.exit(f"could not find set_h_timings/set_v_timings/deckSight_refresh_rates in {path}")
    H_FP, H_SYNC, H_BP = map(int, h.groups())
    V_FP, V_SYNC, V_BP = map(int, v.groups())
    code = re.sub(r"--[^\n]*", "", r.group(1))
    return [int(x) for x in re.findall(r"\d+", code)]

DRM_MODE_CONNECTOR_eDP = 14
DRM_MODE_FLAG_PHSYNC, DRM_MODE_FLAG_PVSYNC = 1 << 0, 1 << 2
DRM_MODE_TYPE_USERDEF = 1 << 5


# ---------------------------------------------------------------------------
# libdrm bindings (only what's needed)
# ---------------------------------------------------------------------------

u32, u16, c_int = ctypes.c_uint32, ctypes.c_uint16, ctypes.c_int
P = ctypes.POINTER


class ModeInfo(ctypes.Structure):
    _fields_ = [("clock", u32), ("hdisplay", u16), ("hsync_start", u16), ("hsync_end", u16),
                ("htotal", u16), ("hskew", u16), ("vdisplay", u16), ("vsync_start", u16),
                ("vsync_end", u16), ("vtotal", u16), ("vscan", u16), ("vrefresh", u32),
                ("flags", u32), ("type", u32), ("name", ctypes.c_char * 32)]


class Res(ctypes.Structure):
    _fields_ = [("count_fbs", c_int), ("fbs", P(u32)), ("count_crtcs", c_int), ("crtcs", P(u32)),
                ("count_connectors", c_int), ("connectors", P(u32)),
                ("count_encoders", c_int), ("encoders", P(u32)),
                ("min_width", u32), ("max_width", u32), ("min_height", u32), ("max_height", u32)]


class Connector(ctypes.Structure):
    _fields_ = [("connector_id", u32), ("encoder_id", u32), ("connector_type", u32),
                ("connector_type_id", u32), ("connection", c_int), ("mmWidth", u32),
                ("mmHeight", u32), ("subpixel", c_int), ("count_modes", c_int),
                ("modes", P(ModeInfo)), ("count_props", c_int), ("props", P(u32)),
                ("prop_values", P(ctypes.c_uint64)), ("count_encoders", c_int),
                ("encoders", P(u32))]


class Encoder(ctypes.Structure):
    _fields_ = [("encoder_id", u32), ("encoder_type", u32), ("crtc_id", u32),
                ("possible_crtcs", u32), ("possible_clones", u32)]


class Crtc(ctypes.Structure):
    _fields_ = [("crtc_id", u32), ("buffer_id", u32), ("x", u32), ("y", u32), ("width", u32),
                ("height", u32), ("mode_valid", c_int), ("mode", ModeInfo), ("gamma_size", c_int)]


drm = ctypes.CDLL("libdrm.so.2", use_errno=True)
drm.drmModeGetResources.restype = P(Res)
drm.drmModeGetConnector.restype = P(Connector)
drm.drmModeGetEncoder.restype = P(Encoder)
drm.drmModeGetCrtc.restype = P(Crtc)
drm.drmModeSetCrtc.argtypes = [c_int, u32, u32, u32, u32, P(u32), c_int, P(ModeInfo)]


def _ioc(nr, size):  # _IOWR('d', nr, size)
    return (3 << 30) | (size << 16) | (ord("d") << 8) | nr


DRM_IOCTL_MODE_CREATE_DUMB = _ioc(0xB2, 32)
DRM_IOCTL_MODE_MAP_DUMB = _ioc(0xB3, 16)
DRM_IOCTL_MODE_DESTROY_DUMB = _ioc(0xB4, 4)


def make_mode(hz):
    ht = H_ACTIVE + H_FP + H_SYNC + H_BP
    vt = V_ACTIVE + V_FP + V_SYNC + V_BP
    m = ModeInfo()
    m.clock = round(ht * vt * hz / 1000)          # kHz
    m.hdisplay, m.hsync_start = H_ACTIVE, H_ACTIVE + H_FP
    m.hsync_end, m.htotal = H_ACTIVE + H_FP + H_SYNC, ht
    m.vdisplay, m.vsync_start = V_ACTIVE, V_ACTIVE + V_FP
    m.vsync_end, m.vtotal = V_ACTIVE + V_FP + V_SYNC, vt
    m.vrefresh = hz
    m.flags = DRM_MODE_FLAG_PHSYNC | DRM_MODE_FLAG_PVSYNC
    m.type = DRM_MODE_TYPE_USERDEF
    m.name = b"1080x1920"
    return m


def find_edp(fd):
    res = drm.drmModeGetResources(fd)
    if not res:
        sys.exit("drmModeGetResources failed")
    r = res.contents
    crtcs = [r.crtcs[i] for i in range(r.count_crtcs)]
    for i in range(r.count_connectors):
        c = drm.drmModeGetConnector(fd, r.connectors[i])
        if not c:
            continue
        cc = c.contents
        if cc.connector_type != DRM_MODE_CONNECTOR_eDP:
            continue
        crtc_id = 0
        if cc.encoder_id:
            enc = drm.drmModeGetEncoder(fd, cc.encoder_id)
            crtc_id = enc.contents.crtc_id if enc else 0
        if not crtc_id:  # pick the first CRTC any of its encoders can drive
            for k in range(cc.count_encoders):
                enc = drm.drmModeGetEncoder(fd, cc.encoders[k])
                if enc:
                    for idx, cid in enumerate(crtcs):
                        if enc.contents.possible_crtcs & (1 << idx):
                            crtc_id = cid
                            break
                if crtc_id:
                    break
        native = [cc.modes[k] for k in range(cc.count_modes)]
        return cc.connector_id, crtc_id, native
    sys.exit("no eDP connector found")


class DumbFB:
    """XRGB8888 colour bars: red, green, blue, white, 50% grey (top to bottom)."""

    BANDS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255), (128, 128, 128)]

    def __init__(self, fd, w, h):
        self.fd = fd
        req = bytearray(struct.pack("<IIIIIIQ", h, w, 32, 0, 0, 0, 0))
        fcntl.ioctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, req)
        _, _, _, _, self.handle, pitch, size = struct.unpack("<IIIIIIQ", req)
        fb = u32()
        if drm.drmModeAddFB(fd, w, h, 24, 32, pitch, self.handle, ctypes.byref(fb)):
            raise OSError(ctypes.get_errno(), "drmModeAddFB failed")
        self.fb_id = fb.value
        mreq = bytearray(struct.pack("<IIQ", self.handle, 0, 0))
        fcntl.ioctl(fd, DRM_IOCTL_MODE_MAP_DUMB, mreq)
        offset = struct.unpack("<IIQ", mreq)[2]
        buf = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_WRITE | mmap.PROT_READ, offset=offset)
        band_h = h // len(self.BANDS)
        for y in range(h):
            r, g, b = self.BANDS[min(y // band_h, len(self.BANDS) - 1)]
            if y % band_h == 0:
                row = bytes([b, g, r, 0]) * w + bytes(pitch - 4 * w)
            buf[y * pitch:(y + 1) * pitch] = row
        buf.close()

    def destroy(self):
        drm.drmModeRmFB(self.fd, self.fb_id)
        fcntl.ioctl(self.fd, DRM_IOCTL_MODE_DESTROY_DUMB, bytearray(struct.pack("<I", self.handle)))


# ---------------------------------------------------------------------------
# Input (keyboard / controller via evdev, plus stdin if it is a terminal)
# ---------------------------------------------------------------------------

EV_KEY, EV_ABS, ABS_HAT0Y = 1, 3, 17
KEY_POWER = 116
KEYMAP = {
    28: "ok", 96: "ok", 304: "ok",                                   # Enter, KP Enter, A
    34: "green",                                                      # G
    46: "other-colour", 103: "other-colour", 544: "other-colour", 307: "other-colour",  # C, Up, dpad up, Y
    31: "static/garbage", 108: "static/garbage", 545: "static/garbage", 308: "static/garbage",  # S, Down, dpad down, X
    48: "black", 33: "flicker",                                       # B, F
    1: None, 16: None, 305: None, KEY_POWER: None,                    # Esc, Q, B button, power -> quit
}
STDIN_MAP = {"": "ok", "g": "green", "c": "other-colour", "s": "static/garbage",
             "b": "black", "f": "flicker", "q": None}
EVENT = struct.Struct("llHHi")


def has_key(dev, code):
    try:
        words = open(f"/sys/class/input/{dev}/device/capabilities/key").read().split()
    except OSError:
        return False
    bits = 64 if struct.calcsize("l") == 8 else 32
    idx = len(words) - 1 - code // bits
    return idx >= 0 and int(words[idx], 16) >> (code % bits) & 1


SKIP_NAMES = ("mouse", "cecd", "dp-", "hdmi", "video bus")


def open_inputs():
    """Keyboards, gamepads and the power button, read straight from evdev so no
    console process can take the keys. Display CEC 'remotes' and mice are
    skipped: they come and go on console switches."""
    fds = {}
    for path in sorted(glob.glob("/dev/input/event*")):
        dev = os.path.basename(path)
        try:
            name = open(f"/sys/class/input/{dev}/device/name").read().strip()
        except OSError:
            continue
        if any(k in name.lower() for k in SKIP_NAMES):
            continue
        if has_key(dev, 28) or has_key(dev, 304) or has_key(dev, KEY_POWER):
            try:
                fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = name
            except OSError:
                pass
    return fds


class Console:
    """The VT we switch to (its login prompt masked for the run). Output only:
    shows the legend and status on any screen fbcon drives, e.g. an external
    monitor. Echo is off so keypresses don't clutter it."""

    LEGEND = ("DeckSight refresh-rate modeset test  (Deck panel shows colour bars)\r\n"
              "  Enter/A = ok     C/Up/Y = colour cast     G = green\r\n"
              "  S/Down/X = static/garbage   B = black   F = flicker\r\n"
              "  QUIT: Esc / Q / B-button / POWER BUTTON / Ctrl+Alt+F1 (desktop is restored)\r\n\r\n")

    def __init__(self, vt):
        self.fd = os.open(f"/dev/tty{vt}", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.saved = termios.tcgetattr(self.fd)
        raw = termios.tcgetattr(self.fd)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        raw[6][termios.VMIN], raw[6][termios.VTIME] = 0, 0
        termios.tcsetattr(self.fd, termios.TCSANOW, raw)
        self.write("\x1b[2J\x1b[H\x1b[?25l" + self.LEGEND)

    def write(self, text):
        try:
            os.write(self.fd, text.replace("\n", "\r\n").encode())
        except OSError:
            pass

    def drain(self):
        try:
            while os.read(self.fd, 256):
                pass
        except OSError:
            pass

    def close(self):
        self.write("\x1b[?25h\ndone, returning to the desktop\n")
        try:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        finally:
            os.close(self.fd)


def read_events(inputs, fd):
    """Non-blocking read; forget devices that were unplugged or revoked."""
    try:
        return os.read(fd, EVENT.size * 64)
    except BlockingIOError:
        return b""
    except OSError:
        print(f"   input device gone: {inputs.pop(fd, fd)}")
        try:
            os.close(fd)
        except OSError:
            pass
        return b""


class VTChanged(Exception):
    pass


def wait_label(inputs, timeout, vt=None, console=None):
    for fd in list(inputs):  # drop presses made while the modeset ran
        while read_events(inputs, fd):
            pass
    if console:
        console.drain()
    watch = list(inputs) + ([sys.stdin.fileno()] if sys.stdin.isatty() else [])
    deadline = time.time() + timeout
    while time.time() < deadline:
        if vt is not None and active_vt() != vt:
            raise VTChanged()      # e.g. Ctrl+Alt+F1: user wants the desktop back
        ready, _, _ = select.select(watch, [], [], min(0.5, max(0.0, deadline - time.time())))
        for fd in ready:
            if fd == sys.stdin.fileno():
                ans = sys.stdin.readline().strip().lower()
                if ans in STDIN_MAP:
                    return STDIN_MAP[ans]
                continue
            data = read_events(inputs, fd)
            if fd not in inputs:
                watch.remove(fd)
            for off in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _, _, typ, code, val = EVENT.unpack_from(data, off)
                if typ == EV_KEY and val == 1 and code in KEYMAP:
                    return KEYMAP[code]
                if typ == EV_ABS and code == ABS_HAT0Y and val in (-1, 1):
                    return "other-colour" if val < 0 else "static/garbage"
    return "timeout"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def active_vt():
    return int(open("/sys/class/tty/tty0/active").read().strip().replace("tty", ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=30, help="modesets")
    ap.add_argument("--lua", default=default_lua(),
                    help="gamescope script to take timings and rates from (default: installed one)")
    ap.add_argument("--rates", type=int, nargs="+",
                    help="subset of refresh rates (default: every rate the script offers)")
    ap.add_argument("--settle", type=float, default=1.5, help="seconds after modeset before snapshot")
    ap.add_argument("--vt", type=int, default=5,
                    help="spare VT to switch to (must be one fbcon draws on: 4-6 on SteamOS)")
    ap.add_argument("--label-timeout", type=float, default=120, help="seconds to wait for a label")
    ap.add_argument("--stop-on-bad", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="show connector, modes and plan only")
    ap.add_argument("--log")
    a = ap.parse_args()

    lua_rates = load_lua(a.lua)
    a.rates = a.rates or lua_rates
    reexec = os.environ.get("DECKSIGHT_INHIBITED")  # second pass under systemd-inhibit
    if not reexec:
        print(f"timings from {a.lua}: H {H_FP}/{H_SYNC}/{H_BP}  V {V_FP}/{V_SYNC}/{V_BP}; "
              f"rates {min(a.rates)}-{max(a.rates)} Hz ({len(a.rates)} modes)")
    for hz in sorted({min(a.rates), 60, max(a.rates)} & set(a.rates)) if not reexec else []:
        m = make_mode(hz)
        print(f"  {hz:3d} Hz: {m.clock / 1000:.2f} MHz  {m.hdisplay} {m.hsync_start} {m.hsync_end} {m.htotal}"
              f"  {m.vdisplay} {m.vsync_start} {m.vsync_end} {m.vtotal}")
    if a.dry_run:
        fd = os.open("/dev/dri/card0", os.O_RDWR)
        conn, crtc, native = find_edp(fd)
        print(f"eDP connector {conn} on CRTC {crtc}; kernel modes: "
              + ", ".join(f"{m.name.decode()}@{m.vrefresh} {m.clock}kHz" for m in native))
        os.close(fd)
        return
    if os.getuid() != 0:
        sys.exit("run as root (sudo)")
    if not os.environ.get("DECKSIGHT_INHIBITED"):
        # Suspending while we hold DRM master hung the Deck on resume. Block sleep and
        # let logind ignore the power key, which this tool then uses as "quit".
        os.environ["DECKSIGHT_INHIBITED"] = "1"
        os.execvp("systemd-inhibit", ["systemd-inhibit", "--mode=block", "--who=drm_modecycle",
                                      "--why=display test holds DRM master",
                                      "--what=sleep:idle:handle-power-key:handle-suspend-key:"
                                      "handle-lid-switch", sys.executable, *sys.argv])

    dev = repro.aux_device()
    path, log = repro.open_log(a.log)
    klog = repro.KernelLog()
    inputs = open_inputs()
    print("label/quit input from: " + (", ".join(sorted(set(inputs.values()))) or "none found"))
    orig_vt = active_vt()
    # Watchdog: if this process dies for any reason, go back to the desktop VT.
    gettys = [f"getty@tty{a.vt}.service", f"autovt@tty{a.vt}.service"]
    unmask = "systemctl unmask --runtime " + " ".join(gettys)
    subprocess.Popen(["sh", "-c", f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 1; done; "
                                  f"{unmask} >/dev/null 2>&1; chvt {orig_vt}"], start_new_session=True)
    # Stop systemd-logind from starting a login prompt on the test VT (it grabbed the
    # keyboard last time). --runtime masks live in /run and vanish on reboot anyway.
    subprocess.run(["systemctl", "mask", "--runtime", *gettys], capture_output=True)
    subprocess.run(["systemctl", "stop", *gettys], capture_output=True)
    print(f"switching VT {orig_vt} -> {a.vt}; the desktop comes back when this finishes")
    subprocess.run(["chvt", str(a.vt)], check=True)
    time.sleep(1.5)
    subprocess.run(["systemctl", "stop", *gettys], capture_output=True)  # in case one raced us

    fd = fb = saved = console = None
    counts = {}
    vt_changed = False
    try:
        console = Console(a.vt)
        fd = os.open("/dev/dri/card0", os.O_RDWR)
        if drm.drmSetMaster(fd):
            raise OSError(ctypes.get_errno(), "drmSetMaster failed (is KWin still active on this VT?)")
        conn_id, crtc_id, _ = find_edp(fd)
        saved_p = drm.drmModeGetCrtc(fd, crtc_id)
        saved = saved_p.contents if saved_p else None
        fb = DumbFB(fd, H_ACTIVE, V_ACTIVE)
        conn = u32(conn_id)
        current = previous = None
        for i in range(1, a.n + 1):
            hz = random.choice([r for r in a.rates if r != current] or a.rates)
            mode = make_mode(hz)
            before = repro.snapshot(dev)
            t0 = time.time()
            rc = drm.drmModeSetCrtc(fd, crtc_id, fb.fb_id, 0, 0, ctypes.byref(conn), 1, ctypes.byref(mode))
            if rc:
                msg = f"[{i}/{a.n}] {hz} Hz rejected by the kernel: {os.strerror(ctypes.get_errno())}"
                print(msg)
                console.write(msg + "\n")
                continue
            previous, current = current, hz
            time.sleep(a.settle)
            after = repro.snapshot(dev)
            kmsgs = klog.new()
            status = f"[{i}/{a.n}] {hz} Hz ({mode.clock / 1000:.2f} MHz)  " \
                     f"{repro.brief(after['decoded'])}"
            print(status)
            console.write(status + "\n   label? ")
            label = wait_label(inputs, a.label_timeout, a.vt, console)
            console.write(f"{label or 'quit'}\n")
            if label is None:
                break
            counts[label] = counts.get(label, 0) + 1
            log.write(json.dumps({"time": t0, "kind": "trigger", "trigger": "drm-refresh", "iteration": i,
                                  "requested_hz": hz, "previous_hz": previous, "clock_khz": mode.clock, "label": label,
                                  "timings": {"h": [H_FP, H_SYNC, H_BP], "v": [V_FP, V_SYNC, V_BP],
                                              "lua": a.lua},
                                  "before": before, "after": after, "kernel": kmsgs}) + "\n")
            log.flush()
            if label not in ("ok", "timeout") and a.stop_on_bad:
                break
    except VTChanged:
        vt_changed = True
        print("console switched away (e.g. Ctrl+Alt+F1): stopping")
    finally:
        if console is not None:
            console.close()
        if fd is not None:
            if saved is not None and saved.mode_valid:
                drm.drmModeSetCrtc(fd, crtc_id, saved.buffer_id, saved.x, saved.y,
                                   ctypes.byref(u32(conn_id)), 1, ctypes.byref(saved.mode))
            if fb is not None:
                fb.destroy()
            drm.drmDropMaster(fd)
            os.close(fd)
        subprocess.run(["systemctl", "unmask", "--runtime", *gettys], capture_output=True)
        if vt_changed:
            # KWin's VT became active while we still held DRM master, so it may not have
            # taken the display back; switching away and back makes it re-acquire.
            subprocess.run(["chvt", str(a.vt)])
            time.sleep(0.5)
        subprocess.run(["chvt", str(orig_vt)])
    print(f"results: {counts}\nlogged to {path}\nsummary: ./greencast_repro.py summary {path}")


if __name__ == "__main__":
    main()
