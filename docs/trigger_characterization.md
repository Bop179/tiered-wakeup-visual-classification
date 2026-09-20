# Tier 1 trigger characterization

**Owner: the Tier 2 owner.** Drafted Sep 19 by the Tier 3 owner from the as-built rig and the repo, so the write-up has a
Tier 1 section for the deadline. Everything below is either measured at the rig or marked
**OUTSTANDING**. the Tier 2 owner: correct the values, fill the gaps you have instruments for, and write §6 in
your own words — that section carries more weight in the write-up than the tables.

> **Provenance.** §1–§3 and §6 describe the rig as it ran the Sep 18–19 overnight matrix. §5 was
> measured on the bench Sep 19 (DMM for Tier 1, FNB58 for the Uno). §4 was never run: the Pi was
> down Sep 13–17 and the one remaining night went to the matrix itself. It is reported as
> not-measured, not estimated. The only rows still unmeasurable are the ATmega-alone ones, which
> need a trace cut this board has not had.

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
| Threshold trimmer | 3386P | **deliberately not metered** | See §4. Decided Sep 19: the absolute wiper voltage is a function of ambient light and the LDR divider, so it does not transfer to another room and is not worth recording as a design constant. The trimmer is a calibration knob, not a spec. |

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

## 4. Threshold sensitivity — three points, not a curve

**No ROC sweep was run.** `data/tier1_roc.csv` does not exist, and `analysis/plots.py` skips the ROC
figure accordingly ("roc: not enough data yet"). What exists instead is a **three-point threshold
comparison** measured Sep 19 for the video's Demo B, at a fixed stimulus (4 real flashes at contrast
0.8 plus low-contrast flicker bait) with `PERSIST` held at 10 ms and capture disabled:

| Trimmer | Comparator blips rejected | Real flashes detected | False wakes |
|---|---|---|---|
| as run overnight | 2, at 4 ms | **4 / 4** | 0 |
| toward threshold (sensitive) | **94, at 0–1 ms** | **4 / 4** | **0** |
| away from threshold (insensitive) | 0 — LED never lights | **0 / 4** | 0 |

Raw: `data/demoB/{normal,sens,lowsens}.{csv,log}` with matching `gen_*.csv` stimulus records for the
first two. The low-sensitivity take has no stimulus record — it was shot before `-o` was added to the
stimulus command — so its "0 detected" rests on the video, not on a logged count of what was shown.

**The result worth keeping: Tier 2's persistence filter absorbs a badly miscalibrated Tier 1.** At
the sensitive setting the comparator tripped 94 times in two minutes on a static screen, every blip
under a millisecond, and `PERSIST 10 ms` discarded all 94 while still passing all four real flashes.
A wrong analog threshold did not produce a single spurious wake. The failure mode the design is
actually exposed to is the opposite one, and it has bitten us: `PERSIST` shipped at 40 ms and was
silently rejecting real LDR flashes until Sep 18.

**This is three points, not a sensitivity curve**, and the threshold between them was set by hand
(bracket where the comparator turns on, bracket where it turns off, sit in the middle). The write-up
must not claim an operating point chosen from a ROC.

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

## 5. Tier 1 and Tier 2 current

Measured Sep 19. Tier 1 with a DMM in series in the 5 V feed from the Uno's `5V` pin to the
breadboard rail; the Uno board with the FNB58 inline in its wall-charger USB lead.

| What | Condition | Current | Voltage | Power |
|---|---|---|---|---|
| **Tier 1 total** | quiescent (black screen) | **2.40–2.43 mA** | 5 V | **~12 mW** |
| **Tier 1 total** | triggered (white patch) | **4.18–4.40 mA** | 5 V | **~21 mW** |
| ATmega328P only | power-down sleep | **not measurable** | 5 V | see below |
| ATmega328P only | awake, idle | **not measurable** | 5 V | see below |
| Uno board + Tier 1 | awake, idle | **31.24 mA** | 5.332 V | **167 mW** |
| Uno board + Tier 1 | power-down sleep | **16.77 mA** | 5.336 V | **89 mW** |
| Uno board alone | awake, idle | ~28.8 mA | 5.33 V | ~154 mW (Tier 1 subtracted) |
| Uno board alone | power-down sleep | ~14.4 mA | 5.33 V | ~77 mW (Tier 1 subtracted) |

**Tier 1 costs about 12 mW watching and 21 mW while triggered.** The ~9 mW difference is mostly the
comparator's indicator LED, which is a debug aid and not part of the design — a deployed Tier 1
would sit at the 12 mW figure.

**The ATmega alone cannot be measured on this board.** Isolating the chip's VCC from the Uno's
regulator, power LED and ATmega16U2 USB bridge means cutting a trace, which was not done. Every
Tier 2 figure here is therefore a **whole-board** figure and must be labelled as such. A microwatt
sleep claim would need a bare ATmega on a breadboard, which this project does not have.

**Sleep entry is visible on the meter, not just inferred.** One continuous log across a power-cycle
shows 31.2 mA flat for 51 s, then a clean step to 16.8 mA at **t = 52 s** — exactly the firmware's
30 s dormancy + 2 s ACK timeout + 20 s `HALT_SETTLE_MS`. The step is what licenses calling the lower
plateau "power-down sleep"; a single reading could not. The sleep plateau is flat to ±0.01 mA over
90 s, and mean(V·I) and dE/dt agree at 89.5 mW.
Method: `tools/fnb58_logger.py`, meter inline in the Uno's wall-USB lead, Pi and Mac both
disconnected (any serial traffic resets the dormancy timer), monitor blacked so Tier 1 stayed idle.
Raw: `data/uno_sleep_awake/power.csv`, `power_sleep.csv`.

**Tier 1 is inside this boundary.** It is fed from the Uno's `5V` pin, so its ~2.4 mA is in both
figures, and the 5 V rail stays live while the MCU is powered down — Tier 1 does not sleep with it.
The "Uno board alone" rows subtract 2.40 mA. That subtraction is slightly conservative: the pin
actually sits near 5.3 V on USB power, not the 5.0 V at which Tier 1 was metered.

**Consequence for the write-up:** every energy number the project reports is still Tier 3 only, and
the savings figures are "Pi rail against an always-on Pi rail". But the cascade's own cost is now
measured, so the open question is answered — **and the earlier guess was backwards.** Weighting the
two plateaus by each cell's halted fraction, Tiers 1+2 cost ~0.11 W against Pi-rail savings of
0.21–0.73 W. The cascade is net-positive at every cell measured. It is cheapest relative to the
saving at **long** intervals (120 s / 30 s: ~15% of a 0.729 W saving), not most expensive: rare
events let the Pi halt more and save more. The squeeze is the opposite corner — short interval, long
dormancy (20 s / 60 s: ~61% of a 0.212 W saving), where the Pi stays awake often and the Uno stays
awake alongside it.

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
