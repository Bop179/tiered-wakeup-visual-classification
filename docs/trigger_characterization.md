# Tier 0 trigger characterization

**Owner: Juan. Due Sep 13.** Fill this in as you build — it is a lab notebook, not a report written
afterwards. Figures from here become the Tier 0 ROC in the final write-up.

---

## 1. Final schematic

*(Photo or drawing of the built circuit with final component values. Replace this line.)*

| Stage | Part | Final value | Notes |
|---|---|---|---|
| Photosensor | | | package type — clear or IR-filtered? |
| Load resistor | | | quiescent output voltage under room light: ___ V |
| High-pass C | | | |
| High-pass R (to 2.5 V) | | | f_c = ___ Hz |
| 2.5 V bias divider | | | bypass cap fitted? |
| Op-amp | | | |
| Gain Rf / Rg | | | G = ___ |
| Low-pass R / C | | | f_c = ___ Hz. Second pole fitted? |
| Comparator | LM339N | | pull-up ___ Ω, hysteresis R ___ Ω |
| Threshold trimmer | 3386P | | |

## 2. Sensor sees the monitor (Sep 7)

*Scope trace of the **raw, unfiltered** sensor output during a patch flash. This is the gate for
everything else — if there is no clear step here, swap the sensor the same day.*

- Sensor used:
- Quiescent level under room light: ___ V
- Step amplitude on a full-contrast patch flash: ___ mV
- Step amplitude with the shroud fitted: ___ mV
- Rise time: ___ ms

## 3. Quiet-window false-trigger rate (Sep 8)

Monitor showing **static black**, room lighting normal, nobody moving. Count comparator firings.

| Trimmer position | Duration | Firings | Rate (/min) |
|---|---|---|---|
| | 5 min | | |

If this will not come down: **cascade a second RC low-pass pole before touching the threshold.**
Lowering sensitivity to fix a noise problem costs real detections too.

## 4. Threshold × contrast sweep (Sep 11–13)

**≥ 6 trimmer positions.** For each, sweep patch contrast and record detections and false triggers.
This is the ROC.

| Trimmer (turns from CCW end) | V_threshold | Contrast | Events shown | Detected | False triggers / min |
|---|---|---|---|---|---|
| | | | | | |

Record `V_threshold` at the wiper with a DMM, not just the turn count — turn counts do not survive
a knock and the wiper voltage is what the comparator actually sees.

## 5. Tier 0 and Tier 1 current (Sep 13)

DMM in series. These are near-constants and only need measuring once; the FNB58 is on the Pi rail.

| What | Condition | Current | Voltage | Power |
|---|---|---|---|---|
| Tier 0 total | quiescent | | 5 V | |
| Tier 0 total | triggered | | 5 V | |
| **ATmega328P only** | power-down sleep | | 5 V | |
| **ATmega328P only** | awake, idle | | 5 V | |
| Uno board | power-down sleep | | 5 V | expect ~20 mA — LED + USB chip |
| Uno board | awake, idle | | 5 V | |

**Report both the ATmega figure and the board figure, and say which is which.** A µW claim next to a
visibly lit Uno invites the one question you cannot answer.

## 6. What surprised me

*Anything that did not behave as the brief said. This section is worth more to the write-up than
the tables — it is where the real engineering shows.*
