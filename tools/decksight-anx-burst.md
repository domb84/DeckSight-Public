# `decksight-anx-burst.py`: live burst-mode DSI experiment

An experimental tool that switches the ANX eDP→MIPI bridge from **non-burst**
to **burst** DSI video mode at runtime, without flashing a BIOS. It exists to
test the leading hypothesis in [`GREEN-CAST-FINDINGS.md`](../GREEN-CAST-FINDINGS.md):
that the colour cross-feed and noise come from r04 running the bridge in
non-burst mode.

> **Status: tested 2026-10-01; runtime burst is not reachable.** The writes
> work and are fully reversible, but the OCM applies `SW_PANEL_INFO_1` only
> when the EC pushes its panel table at bridge power-up, and the EC always
> pushes `0x44`. Plain re-notify, `--rearm`, eDP output disable/enable and a
> suspend/resume (which restores `0x44`) all left the DSI host unchanged.
> Testing burst needs a firmware image; see the live-run section of
> [`GREEN-CAST-FINDINGS.md`](../GREEN-CAST-FINDINGS.md).

## Background

| | `SW_PANEL_INFO_1` (`0x01:0xB1`) | DSI video mode |
|---|---|---|
| Stock Steam Deck LCD firmware | `0x48` = `SET_DPHY_TIMING` + trans_mode 2 | burst |
| DeckSight r04 | `0x44` = `SET_DPHY_TIMING` + trans_mode 1 | non-burst sync events |

The EC writes this register to the bridge as part of its panel table, and
finishes the table with two "panel info ready" notifications:
`MISC_NOTIFY_OCM1` (`0x9F`) = `0x7B`, then `MISC_NOTIFY_OCM0` (`0x9E`) =
`0xC0` (`MCU_LOAD_DONE | PANEL_INFO_SET_DONE`). After those notifications, the
bridge's own firmware (the OCM) derives the DSI lane clock, the line timings
and the D-PHY timing from the panel table.

This tool does exactly that, with one value changed:

1. Write `0x01:0xB1 = 0x48` (or `0x44` to restore).
2. Optionally (`--rearm`), write `0x01:0x9E = 0x80` to clear `PANEL_INFO_SET_DONE`.
3. Write `0x01:0x9F = 0x7B`, then `0x01:0x9E = 0xC0`, in the EC's order.
4. Wait 2 s, then read back and report whether the DSI host registers changed.

Writing the DSI host's `VID_MODE_CFG` mode bits directly would not give real
burst mode. The live `VID_HLINE_TIME` of 931 byte clocks (≈ 1241 px × ¾)
shows the lane clock is sized exactly for non-burst at 24 bpp over 4 lanes, so
there would be no spare capacity. Only the OCM recomputes the clocks, which is
why this tool goes through the OCM instead.

## Safety design

- **Single-byte writes only.** It uses the firmware's `ANW1` transaction
  (command `0x82`, decoded from `bios/live-DSDT.aml`). A 32-bit write at
  `0xB1` would also overwrite `0xB2`–`0xB4`.
- **Hard allowlist.** The write function refuses anything except:

  | Register | Allowed values |
  |---|---|
  | `0x01:0xB1` | `0x44`, `0x48` |
  | `0x01:0x9F` | `0x7B` |
  | `0x01:0x9E` | `0x80`, `0xC0` |

  This matters because slave `0x01` also holds the bridge's **OCM flash
  controller** (`0x0F`–`0x3F`, `0x60`, including erase control). A stray write
  there could erase the bridge firmware, which is the one outcome a reboot
  would *not* fix.
- **Pre-checks.** The tool reads the bridge's live panel table and refuses to
  write unless every register matches r04 (`R04_SPI`, imported from
  `tools/bios/decksight_bios.py`). It also refuses unless `0xB1` holds the
  expected starting value: `0x44` for `burst`, `0x48` for `restore`.
- **Mailbox ownership.** It refuses to run while
  `decksight-brightnessctrl.service` is active, and aborts if the mailbox is
  already busy before a transaction.
- **Dry run by default.** No writes happen without `--apply`, followed by a
  typed confirmation (skip it with `--yes`).

## Reversibility

The written values live in bridge RAM only. The EC's copy of the table is in
the signed BIOS image and is never touched, and the EC rewrites the bridge
registers whenever the bridge powers up.

Restoring the r04 configuration, from least to most disruptive:

| Method | Notes |
|---|---|
| `sudo python3 tools/decksight-anx-burst.py restore --apply` | Needs a readable screen, or run it over SSH. Uses the same OCM re-notify path, so it may not be able to undo a change the OCM didn't apply. |
| Full shutdown, then power on | The sure method; the EC re-runs its complete init. A warm reboot may keep the bridge powered. |
| Hold the power button ~10 s | Forces off when the screen is unusable. |

A real suspend/resume probably also resets the bridge: after S3, its DPCD
capabilities come back as its power-on defaults. This is not proven for this
register, though. **Don't suspend during an experiment**, because it may
silently put the r04 setting back and invalidate the result.

## Usage

Prepare first:

- Have a way to recover if the OLED goes dark. An external USB-C display
  works well: it doesn't pass through the ANX bridge, so this tool can't
  affect it, and you can run `restore` or `poweroff` from a terminal on it.
  SSH also works.
- Keep the internal OLED **enabled** (check `eDP-1` with `kscreen-doctor -o`),
  extended or mirrored. If it's disabled, the bridge's DSI output may not be
  running, so the readings would mean nothing. Re-enabling it later could
  also reset the bridge and undo the change.
- Start from a visually confirmed clean panel: fresh boot, colour bars
  correct.

```sh
sudo systemctl stop decksight-brightnessctrl.service

# Read-only: SW_PANEL_INFO_1, notify state, DSI mode/timing, PLL lock
sudo python3 tools/decksight-anx-burst.py status --log burst-run.jsonl

# Dry run: pre-checks plus the exact writes that would be sent
sudo python3 tools/decksight-anx-burst.py burst

# Real attempt
sudo python3 tools/decksight-anx-burst.py burst --apply --log burst-run.jsonl

# If the result says "OCM did not re-run panel init", try the edge-triggered form
sudo python3 tools/decksight-anx-burst.py burst --apply --rearm --log burst-run.jsonl

# Back to r04 behaviour (or just shut down fully)
sudo python3 tools/decksight-anx-burst.py restore --apply --log burst-run.jsonl

sudo systemctl start decksight-brightnessctrl.service
```

`--log` appends one JSON line per run, with full before/after snapshots of the
panel table and the DSI host registers.

## Reading the result

| Result line | Meaning | Next step |
|---|---|---|
| `write did not stick` | `0xB1` reads back unchanged; the OCM rewrote it or the mailbox write path differs | Stop; record the log |
| `DSI host was not reprogrammed` | The register took, but the OCM ignored the notification | Retry with `--rearm`. If that also does nothing, runtime burst isn't reachable this way, and only a test BIOS image can test it. |
| `OCM reprogrammed the DSI host … video mode is now burst` | Burst is active | Check the panel, then run the reproduction below |

If burst mode takes, repeat the confirmed-clean-start reproduction **without
suspending in between**:

```sh
sudo python3 tools/display/drm_modecycle.py ...   # same seed/sequence as the non-burst baseline
```

Compare it against the documented non-burst baseline. With the same seed, the
fault appeared on request 3 (`59 -> 61 -> 60` Hz). If burst survives
substantially more transitions, that supports the non-burst hypothesis and
justifies building a `0x48` test BIOS image. Note that such an image can't be
validly signed yet (see `tools/bios/decksight_bios.py`), so it needs an SPI
programmer.

## Possible outcomes worth recording

- **Black or garbled screen.** The OLED may not accept burst mode. Wrong video
  timing isn't expected to damage an OLED. Recover with a full shutdown and
  record that the panel needs non-burst.
- **Panel scan shift or roll.** Previously seen with DCS commands. Record it,
  then shut down fully.
- **PHY status byte or `MIPI TX NOT stable` changes after the writes.** The
  bridge is mid-reconfiguration or failed it. Take another `status` reading
  after a few seconds before drawing conclusions. The PHY lock bit (bit 0)
  reads 0 on a working r04 panel (baseline `0x28`: clock lane and lane 0
  active), so it is probably not wired. Compare the whole byte against the
  baseline instead of reading bit 0.
