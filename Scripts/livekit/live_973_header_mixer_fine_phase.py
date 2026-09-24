#!/usr/bin/env python3
"""Live measurement for #973: set_volume and set_pan land on the whole raw position nearest the request.

Usage:  LPM_EVIDENCE_ROOT=/abs/path/outside/repo \
        python3 live_973_header_mixer_fine_phase.py <worktree> <full-40-char-head-sha>

Needs a disposable project open with playback stopped and track 0's automation off. Every call steers by
`target_ref`, every receipt is kept whole, and volume and pan are put back to where they started.

The product is the only reader of the header slider: System Events cannot address Logic's Track Headers
list (see live_543), so observed_raw is the product reading its own write, not an independent witness.
"""

import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evidence as E  # noqa: E402

WT = sys.argv[1] if len(sys.argv) > 1 else ""
HEAD = sys.argv[2] if len(sys.argv) > 2 else ""
if not WT or not HEAD:
    sys.exit(__doc__)

E.REPO = WT
E.BIN = f"{WT}/.build/release/LogicProMCP"
missing = E.have_tools()
if missing:
    sys.exit(f"cannot run: missing {missing}")

VOLUME_POINTS = [
    (0.0, 0.0), (0.1072961373390558, 0.2), (70.0 / 233.0, 0.4), (0.4206008583690987, 0.5),
    (0.48497854077253216, 0.6), (0.8111587982832618, 0.8), (1.0, 1.0),
]

COVERS = ["Sources/LogicProMCP/Channels/AccessibilityChannel+Mixer.swift"]
VOL, PAN = "set_volume", "set_pan"

WHOLE = [
    (VOL, 147), (VOL, 151), (VOL, 98), (VOL, 40), (VOL, 20),
    (VOL, 5), (VOL, 200), (VOL, 229), (VOL, 120), (VOL, 173),
    (PAN, 71), (PAN, 57), (PAN, 0), (PAN, 127), (PAN, 33),
    (PAN, 99), (PAN, 65), (PAN, 63), (PAN, 90), (PAN, 64),
]
FRACTIONAL = [(VOL, 105.5), (VOL, 54.25), (VOL, 82.55), (PAN, 63.5)]
RAIL = [(VOL, 2, 1.25), (VOL, 231, 231.75), (PAN, 125, 125.75)]

MUTATION = ("AccessibilityChannel+Mixer.swift `maxFineSteps = 0`: the fine phase never runs, a target "
            "between detents stops on a detent and reached_exact is false")


def volume_contract(raw):
    position = min(max(raw / 233.0, 0.0), 1.0)
    for (p0, c0), (p1, c1) in zip(VOLUME_POINTS, VOLUME_POINTS[1:]):
        if position <= p1:
            return c0 + (position - p0) / (p1 - p0) * (c1 - c0)
    return 1.0


def contract(control, raw):
    return volume_contract(raw) if control == VOL else (raw - 63.5) / 63.5


def whole(raw):
    return math.floor(raw + 0.5)


ev = E.Evidence(HEAD, os.environ["LPM_EVIDENCE_ROOT"])
d = E.Driver()

win = E.logic_window()
ev.check("973/precondition-logic-window", bool(win), "Logic's Tracks window is on screen",
         f"window={win!r}", None)
if not win:
    d.close()
    print(json.dumps(ev.write(), indent=1)); sys.exit(1)

d.tool("logic_tracks", "select", {"index": 0})
# Polled, because the first read after the server starts can precede the poller: measured on 12.3 it
# was `no_live_track_read_yet` with no rows, and 1.5 s later rows with no `track_ref` at all.
ref = None
for _ in range(20):
    rows = (d.resource("logic://tracks") or {}).get("data") or []
    ref = (rows[0] or {}).get("track_ref") if rows else None
    if ref:
        break
    time.sleep(1)
ev.check("973/precondition-track-0-by-reference", bool(ref),
         "track 0 has a target_ref, so no write depends on header order", f"target_ref={ref!r}", None)
if not ref:
    d.close()
    print(json.dumps(ev.write(), indent=1)); sys.exit(1)

# Waited for before the rail is located or captured, because Logic draws a surface-bank bar on the
# header once it binds this server's MCU ports, and that bar moves the row. Measured on 12.3
# (2026-09-24): the header's pixels changed in the same poll that `isConnected` and
# `registeredAsDevice` turned true, about 4 s after the server started, and changed back when it
# exited. A first capture taken before that differs from every later one with no write involved.
mcu = {}
for _ in range(40):
    state = d.resource("logic://mcu/state") or {}
    mcu = (state.get("data", state) if isinstance(state, dict) else {}).get("connection") or {}
    if mcu.get("isConnected") and mcu.get("registeredAsDevice"):
        break
    time.sleep(0.5)
bound = bool(mcu.get("isConnected") and mcu.get("registeredAsDevice"))
ev.check("973/precondition-the-mcu-surface-is-bound-before-the-first-capture", bound,
         "Logic has bound this server as its control surface, so every capture shows the same header",
         f"isConnected={mcu.get('isConnected')!r} registeredAsDevice={mcu.get('registeredAsDevice')!r}",
         None)
if not bound:
    d.close()
    print(json.dumps(ev.write(), indent=1)); sys.exit(1)

BAND, BAND_SUBJECT = ev.located_band("Tracks header")
ev.check("973/precondition-the-track-header-rail-was-located", BAND is not None and bool(BAND_SUBJECT),
         "the track-header rail, located by the AXDescription it carries",
         f"band={BAND!r} subject={BAND_SUBJECT!r}", None)
if BAND is None:
    d.close()
    print(json.dumps(ev.write(), indent=1)); sys.exit(1)

rec = ev.record_screen(seconds=150)
before = ev.shot("header-before", settle_region=BAND)
readings = []


def write(control, raw, case):
    value = contract(control, raw)
    body = d.tool("logic_mixer", control, {"target_ref": ref, "value": repr(value)})
    body = body if isinstance(body, dict) else {"raw": str(body)}
    readings.append({"case": case, "control": control, "requested_raw": raw, "value": value,
                     "receipt": body})
    return body


original = {}
for control in (VOL, PAN):
    probe = write(control, 173 if control == VOL else 64, "baseline")
    original[control] = probe.get("observed_before")
ev.note("973/original-contract-values", original)

try:
    for control, raw in WHOLE + FRACTIONAL:
        case = "whole" if float(raw).is_integer() else "fractional"
        body = write(control, raw, case)
        ok = (body.get("state") == "A" and body.get("reached_exact") is True
              and body.get("observed_raw") == whole(raw))
        ev.check(f"973/{control}-{raw}-lands-on-the-nearest-whole-raw", ok,
                 f"State A, reached_exact true, observed_raw {whole(raw)}",
                 f"state={body.get('state')!r} reached_exact={body.get('reached_exact')!r} "
                 f"observed_raw={body.get('observed_raw')!r} nudge={body.get('nudge_steps')!r} "
                 f"fine={body.get('fine_steps')!r}", MUTATION)

    for control, start, raw in RAIL:
        first = write(control, start, "rail-start")
        body = write(control, raw, "rail")
        ok = (first.get("observed_raw") == start and body.get("state") == "A"
              and body.get("reached_exact") is True and body.get("observed_raw") == whole(raw))
        ev.check(f"973/{control}-{start}-to-{raw}-covers-a-detent-reversed-off-a-rail", ok,
                 f"from raw {start}, State A, reached_exact true, observed_raw {whole(raw)}",
                 f"start_raw={first.get('observed_raw')!r} state={body.get('state')!r} "
                 f"reached_exact={body.get('reached_exact')!r} observed_raw={body.get('observed_raw')!r} "
                 f"nudge={body.get('nudge_steps')!r} fine={body.get('fine_steps')!r}",
                 "AccessibilityChannel+Mixer.swift `maxFineSteps = 8`: the rail case stops one write short")

    moved = ev.shot("header-after-writes", settle_region=BAND)
    ev.visual("973/the-header-sliders-moved", before["file"], moved["file"], BAND, subject=BAND_SUBJECT,
              expect_change=True,
              why="the run leaves volume at raw 232 and pan at 126 from about 173 and 64, so the header's own "
                  "pixels must differ; this is the witness that does not go through the product's readback")
finally:
    restored = {}
    for control in (VOL, PAN):
        if isinstance(original.get(control), (int, float)):
            body = d.tool("logic_mixer", control, {"target_ref": ref, "value": repr(original[control])})
            restored[control] = body.get("observed_after") if isinstance(body, dict) else None
ev.restored("973/volume-and-pan-put-back",
            all(isinstance(restored.get(c), (int, float)) and abs(restored[c] - original[c]) < 0.01
                for c in (VOL, PAN)),
            f"original={original!r} restored={restored!r}")

ev.visual("973/the-header-is-back", before["file"], ev.shot("header-restored", settle_region=BAND)["file"],
          BAND, subject=BAND_SUBJECT, expect_change=False,
          why="volume and pan were written back to their starting values, so the rail must match its "
              "first capture")
ev.stop_recording(rec)

ev.note("973/readings", readings)
d.close()
out = ev.write()
print(json.dumps(out, indent=1))
sys.exit(0 if E.is_clean(out) else 1)
