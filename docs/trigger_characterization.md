# Tier 1 trigger characterization

**Owner: the Tier 2 owner.** Drafted Sep 19 by the Tier 3 owner from the as-built rig and the repo, so the write-up has a
Tier 1 section for the deadline. Everything below is either measured at the rig or marked
**OUTSTANDING**. the Tier 2 owner: correct the values, fill the gaps you have instruments for, and write §6 in
your own words — that section carries more weight in the write-up than the tables.

> **Provenance.** §1–§3 and §6 describe the rig as it ran the Sep 18–19 overnight matrix. §4 and §5
> were never run: the Pi was down Sep 13–17 and the one remaining night went to the matrix itself.
> They are reported as not-measured, not estimated.

---

## 1. Final schematic

*(Photo of the built breadboard still to be taken — s17e. Take it before the rig is moved again.)*

| Stage | Part | Final value | Notes |
|---|---|---|---|
| Photosensor | **LDR (CdS cell)** | — | **Not** the PT204-6B. See §2. |
| Load resistor | **Trimmer potentiometer** | set in situ, ~4.7 kΩ nominal | Was a fixed 4.7 kΩ; replaced when the rig moved rooms. See §6. |
| High-pass C | 1 µF | | per brief |
| High-pass R (to 2.5 V) | 1 MΩ | f_c ≈ 0.16 Hz | per brief |
| 2.5 V bias divider | 2 × 10 kΩ + 10 µF | | bypass cap fitted |
| Op-amp | LM358 | | **appears to be wired inverting** — OUTSTANDING, confirm |
| Gain Rf / Rg | 100 kΩ / 10 kΩ | G ≈ 11 | nominal; not verified against a measured step |
| Low-pass R / C | 33 kΩ / 0.1 µF | f_c ≈ 48 Hz | single pole; no second pole fitted |
| Comparator | LM339N | pull-up 10 kΩ | |
| Hysteresis R | | **~100–220 kΩ** | OUTSTANDING — exact value not recorded. Brief specified 1 MΩ. |
| Threshold trimmer | 3386P | **OUTSTANDING** — wiper voltage never metered | See §4. |

**OUTSTANDING for the Tier 2 owner:** confirm the op-amp topology (inverting or non-inverting), read the actual
hysteresis resistor off the board, and meter the threshold wiper to the millivolt.

## 2. Sensor sees the monitor

The PT204-6B was **not used**. It was replaced by an LDR in the divider the brief gives as the
fallback (§0.2 of `TEAMMATE_BRIEF.md`): 5 V → LDR → node → load → GND. More light ⇒ higher node
voltage, the same polarity as the phototransistor, so nothing downstream changed.

- **Sensor used:** LDR (CdS photoresistor)
- **Peak ADC value on a patch flash** (`EVT` field 3, read at A0 through the low-pass): **~645**
  after the divider was re-tuned; **~550** before. Idle sits near 512.
- Quiescent level under room light: OUTSTANDING (DMM)
- Step amplitude, raw sensor, full contrast: OUTSTANDING (scope)
- Step amplitude with shroud: OUTSTANDING (scope)
- Rise time: OUTSTANDING (scope)

The ADC peaks are the only quantitative record of the sensor's response, and they are taken *after*
the whole analog chain, not at the raw junction. A scope trace at the raw junction is still missing.

## 3. Quiet-window false-trigger rate

Monitor showing static black, room lighting normal, nobody moving, Uno on USB.

| Trimmer position | Duration | Firings | Rate (/min) |
|---|---|---|---|
| as locked for the matrix | 5 min (300 s) | **1** | **0.2** |

`# t2: noise` lines: **none**. Run at `PERSIST = 10 ms`.

This passes the gate in the runbook (≤ 1/min). Over a 45-minute cell that is ~9 phantom events
against 40 real ones, which is the right order to keep in mind when reading detection rates.

> **Caveat:** `tools/trigger_patch.py quiet` prints to stdout and the run was not teed to a file, so
> there is no artifact in `data/` behind this row — it is an observation at the rig on the evening of
> Sep 18. Re-run it with `| tee` if the write-up needs a citable number.

## 4. Threshold × contrast sweep — NOT RUN

**No ROC was measured.** `data/tier1_roc.csv` does not exist, and `analysis/plots.py` skips the ROC
figure accordingly ("roc: not enough data yet").

The sweep needed ~20 minutes per trimmer position across ≥ 6 positions, about two hours with the
monitor and rig in their locked measurement state. The Pi was down Sep 13–17 with a loose camera
ribbon, and the single remaining night went to the 10-cell matrix. The sweep was cut with the
duration sweep, INT8-vs-FP32, and latency-by-state.

The threshold was instead set by a **single-point bracketing method**: find the divider setting where
the comparator LED just turns on, find where it just turns off, and set the pot halfway between.
That yields a working threshold but no sensitivity/false-trigger curve, so **the write-up cannot
claim an operating point chosen from a ROC.** It should say the threshold was bracketed by hand.

**To recover this** (~2 h at the rig, monitor and shroud untouched):

```
.venv/bin/python tools/trigger_patch.py sweep --trimmer <wiper V> --serial /dev/cu.usbserial-110
```

## 5. Tier 1 and Tier 2 current — NOT MEASURED

No DMM was in series with either board at any point. The FNB58 sits on the Pi rail only, so it
measures Tier 3 and nothing else.

| What | Condition | Current | Voltage | Power |
|---|---|---|---|---|
| Tier 1 total | quiescent | OUTSTANDING | 5 V | |
| Tier 1 total | triggered | OUTSTANDING | 5 V | |
| ATmega328P only | power-down sleep | OUTSTANDING | 5 V | |
| ATmega328P only | awake, idle | OUTSTANDING | 5 V | |
| Uno board | power-down sleep | OUTSTANDING | 5 V | expect ~20 mA — LED + USB chip |
| Uno board | awake, idle | OUTSTANDING | 5 V | |

**Consequence for the write-up:** every energy number the project reports is Tier 3 only. The
savings figures are "Pi rail against an always-on Pi rail". Tiers 1 and 2 are a real, unmeasured
addition to system power, and the report must say so rather than implying the cascade is free. The
Uno alone plausibly costs more than the savings at long intervals — that claim cannot be settled
without this table.

## 6. What surprised us

**`PERSIST` had to come down from 40 ms to 10 ms.** The firmware required the comparator to hold for
40 ms before accepting a trigger, which is fine for a pushbutton and wrong for this sensor: real LDR
flashes were being rejected as noise. At 10 ms real flashes are accepted and the 5-minute quiet test
still passes at 1 false trigger. The constant carries the comment
`/* 40 until Sep 18: rejected real LDR flashes */`.

**A smaller trigger patch broke the comparator, not the sensor.** The patch was reduced from 0.25 to
0.18 of the screen's short edge to give the stimulus images more room. Detection collapsed: the LDR
only partly overlapped the smaller square, so the edge it saw was slow, and a slow edge through a
comparator with too little hysteresis chatters — the log filled with `noise 0–9 ms` and accepted zero
triggers. Restored to 0.25. **A partly-illuminated sensor is a hysteresis problem, not a gain
problem**, and it is worth knowing that the failure looks identical to a dead sensor from the logs.

**Moving the rig to a darker room moved the operating point.** The fixed 4.7 kΩ load put the
quiescent voltage in the wrong place once the ambient light changed, so it was replaced with a
potentiometer. Re-tuned, the ADC peak went from ~550 to ~645 — better margin over the 512 idle.
The lesson is that the divider is an **ambient-light calibration**, not a design constant, and any
change of room means re-tuning it.

**Back-to-back images merge into one trigger.** Tier 1 sees brightness, not image boundaries. When
one stimulus image is followed immediately by the next, the patch never returns to black between
them, so the comparator never re-arms and the two events produce a single `EVT`. This puts a hard
ceiling on detection that no amount of threshold tuning removes: the never-halt cells, where the Pi
is awake and ought to catch everything, top out at **0.875–0.975**, not 1.0. This is the single
biggest limitation of the Tier 1 design as built. A minimum inter-image gap in the stimulus schedule
would fix it; the deadline came first.

**The wake line's resistor value mattered more than expected.** 1 kΩ against the Pi's 1.8 kΩ pull-up
never pulls GPIO3 low. It needs 330 Ω. This is in `INTERFACE.md` §2.1 but it is the kind of thing
that reads as a detail until the Pi silently refuses to wake.
