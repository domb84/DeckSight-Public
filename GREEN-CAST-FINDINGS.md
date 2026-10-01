# DeckSight Green Cast / Corruption — Findings Summary

Consolidated findings from manual reproduction testing of the intermittent
colour cast / corruption issue on the DeckSight OLED display.

## Manual reproduction results

Triggers tried, with success rates:

| Trigger | Result |
|---|---|
| DPMS off/on | 0/30 — never reproduced it |
| Real suspend → resume | 3/20 (pink/yellow cast — blue channel missing) |
| Rapid refresh-rate changes via custom DRM modesets (40–80 Hz, colour-bar test pattern) | Fastest, most reliable repro — multiple casts and static per 40-cycle run |

Key behavioural findings:

- **The bad state is sticky/latched, not transient.** Once triggered, it
  persists through further modesets and DPMS cycles. In one 40-modeset run:
  only 1 of 4 modesets from a good picture broke it, and only 1 of 35
  modesets from a bad picture cleared it. It can also **escalate** — one run
  went from colour cast to full static, which then itself stuck.
- **Only a real suspend→resume reliably clears it.** DPMS and KWin's own
  60 Hz modeset do not.
- **The corruption is moving noise with a dropped colour channel**, not a
  frozen frame — confirmed directly by visual observation.

## Relationship to the MIPI bridge, and why the fault sits downstream of it

Signal path: **APU → eDP link → ANX bridge (eDP-in / MIPI-DSI-out) → OLED panel.**

Everything on the *upstream* side of the bridge — the eDP link itself and the
GPU's output — checked out identically healthy in every sample taken, good
state or bad:

- **eDP link (DPCD):** 2 lanes × 2.7 Gbps, trained, locked, aligned, zero
  symbol errors — in every single capture, regardless of what the panel was
  showing.
- **amdgpu's output:** 8 bpc, sRGB colourspace, consistent link settings —
  again identical in good and bad states.
- **eDP debugfs status** (PHY settings, PSR/Replay state, DSC, backlight):
  nothing abnormal while the panel was visibly in a static+yellow-cast state.
- The one real bandwidth-related finding — amdgpu silently dropping to 6-bit
  colour at ≥75 Hz because the 2×2.7 Gbps link can't carry 8-bit at that
  pixel clock — is a separate, well-understood phenomenon, not the cast
  itself (the cast also happens at low refresh rates with plenty of
  bandwidth margin).
- **DRM CRC capture** (the standard kernel mechanism for verifying GPU
  output integrity) was attempted but came back **inconclusive**: zero CRC
  records across every available source, despite visibly observing moving
  noise on the panel at the time. That contradiction was never resolved —
  either this hardware/pipeline configuration doesn't support CRC capture,
  or there's a limitation in how the debugfs interface was being read.
  Abandoned as a dead end rather than a finding either way.

**Conclusion:** since nothing upstream of the bridge ever shows a fault, the
problem has to be **inside the bridge's internal state/reformatting logic,
on its MIPI-DSI output, or in the panel's own handling of that DSI stream**
— i.e., downstream of (or within) the bridge, not in the eDP link feeding it.

One concrete, logged anomaly that fits this picture: **after a real
suspend/resume, the bridge's advertised DPCD capability jumps to 4 lanes /
5.4 Gbps (its own hardware power-on defaults) instead of the 2×2.7 Gbps r04
programs at boot** — meaning resume takes a different init path than cold
boot and doesn't fully reapply the boot-time bridge configuration. (The
*active* trained link stayed at 2×2.7G in every sample regardless, so this
is a capability-announcement mismatch, not proof the active link itself
misbehaves — but it's the strongest concrete lead for *why* resume-type
events are where the fault surfaces.)

## Direct mailbox experiment (2026-10-01)

Bounded direct reads through the EC mailbox are now confirmed. During a live
colour fault the bridge reported a stable MIPI TX, idle command FIFO, zero in
both 32-bit DSI error latches, RGB888 pixel coding, and a 1080-pixel DSI video
packet.

The colour bars refined the visual symptom: it is a deterministic cyclic
cross-feed rather than a dropped component. Red becomes yellow, green becomes
cyan, blue becomes magenta, while white and grey remain neutral.

One active attempt to request panel DCS status changed the scan mapping while
leaving the colour cross-feed unchanged: the image top appeared at the bottom
and the remaining image shifted upward. This proves that the DSI command-path
experiment touched a live scan/position state but not the colour-fault state.
The native `CMD_MODE_CFG` was subsequently read as `0x010f7f00`, exactly the
value used by the experiment, ruling out replacement of that register as the
cause. The remaining possibilities are the queued DCS/BTA transaction itself,
its missing response handling, or panel-specific command interpretation.

Static analysis of the production brightness controller confirms the basic
mailbox write format. It writes a three-byte brightness payload to `0xC0:0x70`
and then the little-endian DCS-long-write header `0x00000339` to `0xC0:0x6C`.
The diagnostic utility retains an explicit `--panel-dcs-read` experiment, but
it is known to alter panel scan state and must not be used as a diagnostic or
recovery action. Future command writes require a verified ANX command-mode/BTA
sequence; read-only register snapshots remain appropriate.

The signed r04 image was unpacked with `uninsyde` and the official UEFIExtract
NE A75. This recovered byte-identical copies of the `ANX7580OCMflash` and
`OemAcpiPlatform` EFI drivers. The ANX module is bridge-firmware flash logic;
the OEM ACPI module is an ACPI-table loader. Neither contains a verified panel
DSI read/BTA receive sequence or an embedded DSDT/SSDT payload. Full recursive
extraction stops at malformed/overlapping proprietary Insyde container nodes,
so the installed AML table remains the missing authoritative artifact.

The live DSDT was subsequently captured and decompiled with ACPICA `iasl`.
Its `RDDI (Arg0)` method establishes the intended read sequence: write
`0x00000137` to `GEN_HDR` (`0x6C`), write `(Arg0 << 8) | 0x06` to the same
register, wait 20 ms, then read four bytes from `0xC0:0x70`. It contains a
specific routing bug: both header writes target slave `0x0C`, while the receive
read targets `0xC0`. The native `CMD_MODE_CFG` is `0x010f7f00` and native
`VID_MODE_CFG` is `0x00003f01`. The prior active experiment incorrectly set
the video-mode low-power-command bit (`0x0000bf01`); `RDDI` does not alter that
register. Therefore its scan shift cannot be attributed to the native command
sequence itself, and any corrected experiment must preserve the video-mode
register exactly and use the firmware's 20 ms wait rather than status polling.

The corrected sequence was sent once to MIPI port 0 for DCS `Get Power Mode`
(`0x0A`) and returned `0x0000009c`. This is a valid panel response: booster
on, sleep out, normal display mode, and display on. It proves that the command
reaches the OLED and that response data is available through `0xC0:0x70`.
However, the image scan rolled again while the cyclic colour cross-feed was
unchanged. A direct DCS read is therefore an active panel operation on this
hardware, not a harmless diagnostic; do not issue further reads until the
source of the scan-state side effect is understood.

Read-only timing capture identifies the missing command margin. The host is in
video mode (`MODE_CFG = 0`) with LP transfers enabled in all six video blanking
regions (`VID_MODE_CFG = 0x00003f01`), but `DPI_LP_CMD_TIM = 0` and
`BTA_TO_CNT = 0`. It has no explicit LP-command time allocation or BTA timeout
programmed for traffic injected into the live video stream. The active DSI
timing is 1920 lines with vertical `vsa/vbp/vfp = 1/15/20`; horizontal values
are `hsa/hbp/hline = 1/18/931` in host byte-clock units. This explains how a
DCS read can receive a valid response while still disrupting scan alignment:
the command is not framed by a verified runtime video-to-command transition.
Do not use direct panel DCS as a recovery path; the remaining viable software
recovery is the complete firmware suspend/resume initialization sequence.

That recovery is intermittent, not deterministic. A real suspend/resume has
recovered the panel, but another run cleared the colour cast while leaving full
random image noise; a later identical run recovered it again. A fresh
full-width status snapshot in that noise state was identical to the
colour-cross-feed snapshot: stable TX, empty command FIFO, RGB888, unchanged
video and timing registers, and zero in both 32-bit DSI error latches. Thus the
exposed DesignWare host does not represent the failure even after the complete
OS suspend path; the state is internal to the ANX pipeline or the OLED panel
beyond these registers. The differing outcomes from the same resume sequence
also support a resume-time initialization race or marginal lock condition over
a persistent host configuration error.

In that noise state, the static colour bars from `drm_modecycle.py` revealed a
left-to-right noise crawl. Its speed changed when the test changed refresh
rate. The test keeps resolution and porch geometry fixed, varying only the
pixel clock and frame cadence. This rules out static GPU-framebuffer corruption
and is consistent with a horizontal pixel/byte phase slip in the ANX DSI
serializer or line FIFO, or in the panel receiver. It does not yet distinguish
between those components, but it is more specific than an unclocked or stuck
panel image.

A seeded direct-modeset baseline in the narrow 58--62 Hz range reproduced the
colour fault on its third request: `59 -> 61 -> 60` Hz. The final 60 Hz state
was labelled `other-colour`; its eDP snapshot remained normal (2 lanes at 2.7
Gbps, aligned, no reported errors, D0, PSR disabled). KWin's restoration
modeset after the test sometimes clears the symptom, as it did in this run;
that is an existing intermittent recovery, not a change made by the test.

An A/B stepped-transition attempt falsified the simple rate-ramp mitigation.
With the same target sequence and a maximum 1 Hz step plus a one-second dwell
after every intermediate modeset, the panel again entered the `other-colour`
state on the fourth final target (62 Hz, reached through `61 -> 62`). Thus a
smaller pixel-clock discontinuity and a basic post-modeset settle delay do not
prevent the fault. Since the tool labels only after the final step, it cannot
say whether that 1 Hz transition or the final 62 Hz state was the immediate
trigger; it does show that any simple stepped-rate policy is insufficient.

A reshaped vertical sync was tested next: `vfp/vsync/vbp = 3/15/60` instead of
the standard `3/14/61`, keeping `vtotal` and each requested rate's pixel clock
unchanged. Using the same seed, this survived the `59 -> 61 -> 60` sequence
that broke the standard timing, so the reshape changed the outcome of that
specific transition. The run still reached `other-colour` by request 7
(`60 -> 59 Hz`, a 1 Hz change). The operator initially reported visible
corruption from request 5 onward (`62 -> 58 Hz`, a 4 Hz change), which would
mean the label at request 7 only confirmed a state that was already latched —
consistent with the documented sticky/latched behaviour, where `--stop-on-bad`
records the confirmation point rather than necessarily the trigger point.
However, the operator flagged this real-time observation as unverified and is
re-running the test, so request 5 as the trigger is a hypothesis, not a
finding. Future timing experiments should have the operator call out the exact
iteration number verbally the moment corruption appears, rather than only
entering a label at the next prompt, and that report should be treated as
provisional until repeated.

A follow-up run combined the reshaped vsync with a 1 Hz maximum step and a
one-second intermediate dwell, but was started without first confirming the
panel was in a good state — it was left corrupted from the previous run. Every
label in that run therefore reflects a pre-existing latched state rather than
a fresh reaction to its transitions, and it must not be used as evidence for
or against either mitigation. Any timing or stepping experiment must begin
from a visually confirmed good state (fresh reboot, or a visually verified
`rtcwake` recovery), and that confirmation should be noted alongside the
result.

The same combined test was repeated starting from a panel the operator
visually confirmed was clean. It reached `other-colour` by request 7
(`60 -> 59 Hz`, a direct 1 Hz step under the active `--max-rate-step 1`
ramp). This is a confirmed result: reshaping vsync to `3/15/60` and limiting
every transition to 1 Hz with a one-second dwell, combined, still did not
prevent the fault. Neither mitigation, alone or combined, has prevented
corruption in a confirmed-clean-start test. The remaining candidates are a
porch/timing geometry not yet tried, a magnitude- or direction-dependent
trigger unrelated to step size, or a fault that is not actually caused by the
DRM modeset itself but by something state it disturbs (e.g. link retraining
timing) that the current overrides do not control.

Every timing test so far has used `DeckSight.lua`'s custom porches as its
starting point, including the vsync reshape. Those are not a verified-good
reference: the firmware's own panel/OCM register (`tools/bios/decksight_bios.py`,
offset `0x69ae`) programs a different native timing — `HFP/HSYNC/HBP =
136/1/24`, `VFP/VSYNC/VBP = 20/1/15`, nominal 80 Hz — with much narrower sync
pulses than the Lua script's `48/32/80` and `3/14/61`. The Lua script's own
comment ("vsync increase seems to improve init sync") suggests its wider
blanking was already an empirical workaround attempt, not a verified-correct
timing. No test run so far has used the panel's actual native timing. This
matters regardless of Game Mode or Desktop: `drm_modecycle.py` performs raw
DRM modesets directly, independent of gamescope, so it is not a Game-Mode-only
test — it reproduces the same class of full-modeset event implicated in the
documented Desktop-side trigger ("changing display modes, including Game UI to
Desktop"), just driven by this harness instead of by a session transition.

Attempting to drive the eDP host link directly with that native timing failed
outright: `amdgpu` rejected every single modeset attempt at 60 Hz with
`ENOMEM`, not intermittently. This is a negative result about the experiment
itself, not about the fault. Its `HSYNC=1`/`VSYNC=1` (single pixel/line) sync
pulses are almost certainly too narrow for the eDP link's own host-side mode
validation, and more fundamentally, offset `0x69ae` most likely programs the
bridge's downstream MIPI/DSI-side panel timing — what the ANX generates to
drive the OLED — not a valid upstream eDP mode for the GPU to emit. The host
and panel sides are different clock domains joined by the bridge's own
conversion logic, so this native timing cannot be fed to the eDP link
directly to test it. Testing the upstream/downstream timing-relationship
hypothesis requires either widening the native sync pulses to the minimum the
eDP link will accept while keeping total line/frame length close to native, or
abandoning direct host-side replication of the DSI-side register in favour of
further EC-firmware analysis of how it configures the bridge's input-side
(eDP) timing to match that fixed output.

Static analysis of the EC firmware's OCM table found a cross-validated,
concrete lead. `tools/bios/decksight_bios.py` writes OCM register `0xb1`
(`SW_PANEL_INFO_1`) as `0x44` for DeckSight, noting the stock Steam Deck LCD
BIOS used `0x48`. Per `chicago_registers.h`, bits `[3:2]` of that register are
`REG_PANEL_TRANS_MODE`. Decoding: DeckSight's `0x44` sets trans_mode `1`
(non-burst sync-events); stock's `0x48` sets trans_mode `2` (burst) — standard
DesignWare DSI video-mode encoding. This matches the live register read
captured earlier in this session: `VID_MODE_CFG = 0x00003f01`, whose low two
bits (`01`) are also mode `1`. The firmware table and the live hardware state
agree exactly, confirming this is the field in question and that the firmware
genuinely programs non-burst sync-events mode for the OLED panel, diverging
from the stock LCD's burst mode.

This is architecturally significant: non-burst DSI modes carry far less
timing slack than burst mode. Burst mode's DSI clock runs faster than the
active-video bandwidth requires, so blanking-interval and line-length
mismatches between the bridge's eDP input and its DSI output can be absorbed.
Non-burst modes must track the input's blanking/active timing much more
tightly, matching every other observation: refresh-rate and timing changes
can trigger the fault, the symptom is a frame-relative phase/colour effect
rather than a protocol violation, and DSI error latches and command-FIFO
status remain clean throughout, since a framing/phase slip in a non-burst
pipeline is not necessarily flagged as an error condition by this controller.

### Hypothesis: byte-level alignment slip in a non-burst pipeline

The cyclic cross-feed (red->yellow, green->cyan, blue->magenta, white/grey
unchanged) is consistent with a one-byte shift in an RGB888 stream (3 bytes
per pixel: R, G, B in sequence). A one-byte rotation makes every channel
read the next channel's data; equal-valued white/grey bytes are unaffected.
Burst mode's idle period after each line's active data gives slack to absorb
a host/bridge line-length mismatch every line; non-burst mode has no such
slack, so a persistent mismatch (the host's DRM `htotal` does not match the
panel's documented native line length) creeps forward by roughly a byte per
line instead of being reset. The creep rate is set by the relationship
between the host pixel/byte clock and the bridge's internal generator clock,
which explains why the observed noise-crawl speed changed with refresh rate.
This does not trip any DSI error latch because packet-level checks (header
ECC, payload length, timeouts) do not validate pixel content semantics; a
byte-shifted but correctly-sized payload is protocol-valid. If the creep is
unbounded, it eventually misaligns actual packet boundaries rather than just
pixel bytes within a line, which would present as full static/noise instead
of a clean colour rotation, consistent with documented escalation from cast
to static. This is a coherent explanatory model, not a confirmed mechanism;
the bridge's internal FIFO/alignment state is not observable from the OS.

### Panel identity and burst-mode support are unknown

The EDID vendor ID decodes to `DSO`/`0x5001` with descriptor text literally
`"DeckSight"` (`edid/decksight_edid.bin` and `tools/bios/r04_edid.bin`,
confirmed by decoding the standard EDID 5-bit manufacturer-ID encoding). This
is a self-authored identity ShadeTechnik writes into the BIOS, not a
registered PnP ID for the real OEM panel supplier, and no panel model/part
number string exists elsewhere in this repository. Whether the physical OLED
panel supports MIPI DSI burst mode at all is therefore unknown. Burst and
non-burst are both standard, panel/TCON-dependent options; many OLED panels
(particularly ones derived from phone-class parts) are designed primarily
for DSI command mode with self-refresh rather than continuous burst video
mode, so ShadeTechnik's non-burst choice could be a required configuration
rather than an arbitrary or mistaken one. Do not assume switching to burst
mode is safe without the panel's datasheet; an unsupported mode could produce
a persistently broken display rather than an intermittent one, and testing it
requires a signed-firmware bypass or SPI reflash, both out of scope without
further authorization.

This does not yet prove non-burst mode is the defect; it may simply be
required by this panel. The concrete next questions are whether the OLED
panel supports burst mode at all, and if so, whether a firmware change to
`SW_PANEL_INFO_1` trans_mode (EC offset `0x69ae + 0xb1`, live register
`0xc0:0x34` low bits) to burst mode reduces or removes the corruption. This
would need to be evaluated against the panel's own DSI capability (not
assumed), and tested only via a flashable BIOS image, not a live register
write while video is active.

## Handoff and next investigation

The EC mailbox is confirmed usable from the OS, but it is shared with
`decksight-brightnessctrl`; concurrent access can interleave its scratch
registers and is unsafe. A future software control service must become the
single mailbox owner or explicitly serialize access with the brightness
controller.

The tested software prevention policy is insufficient. The direct baseline
failed at `59 -> 61 -> 60` Hz, and a seeded ramp using maximum 1 Hz steps plus
one-second intermediate dwells also failed. Do not infer that smaller or slower
DRM transitions prevent this issue. A fixed refresh rate may still reduce how
often a user requests a transition, but it is not a demonstrated cure.

The next read-only engineering task is to determine whether the OLED panel
supports MIPI DSI burst mode, and if so, whether switching `SW_PANEL_INFO_1`
trans_mode from `1` (non-burst sync-events) to `2` (burst) in a test firmware
image removes the fault, since non-burst mode is the leading architectural
hypothesis. If the panel requires non-burst mode, continue tracing the two
embedded 8051 EC firmware copies around the `0xCE` S3-resume command and
`s3_turn_on_eDP` to find how firmware configures the bridge's eDP input timing
to match its fixed non-burst DSI output, and whether the ANX repeats that
configuration on every eDP retrain rather than only at boot. Only an
identified, firmware-approved reset/reinitialization sequence should be
considered for a serialized runtime service or a firmware
patch. Do not send guessed ANX or panel writes while video is active.
