# Report draft: measured results

Draft for the Sep 19 write-up. Every number names its run. §1 and §2 are final; §3 is
pre-registered before the overnight matrix (Sep 18) and gets its measured column afterwards.

## 1. What the Pi costs in each state

Pi 4 Rev 1.5, FNB58 in line at 100 Hz, `throttled=0x0` throughout. Case fans on (see 1.3).

| Constant | Estimate | Measured | Run |
|---|---|---|---|
| `P_idle` (daemon idle, camera open) | 2.5 W | **3.26 W** | Gate 0.4, reproduced at 3.23 W |
| `P_halt` (`sudo halt`, wake armed) | 0.5 W | **1.997 W** | Gate 0.4, 1874 s over 3 windows; s18_tboot 1.993 W |
| `T_boot` (wake → daemon `# ready`) | 30 s | **20.8 s** | s18_tboot n=3 (20.66–20.92), s12d n=1 (20.8) |
| `P_boot` | 3.5 W | **3.27 W** | s18_tboot |
| `E_boot` wall | ~100 J | **68.1 J** | s18_tboot n=3 (67.6–68.4) |
| `E_boot` net of `P_halt` | – | **26.6 J** | s18_tboot n=3 (26.4–26.8), s12d 26.6 J |
| `E_infer` (INT8, net of idle) | 0.25 J | **61.8 mJ** | `data/infer/`, 3000 frames |
| Inference latency (INT8, end-to-end p50) | ~100 ms | **20.7 ms** | Gate 0.3, n=200 |
| Held event → result after a wake | – | **42–49 ms** | s12c, s12d, s18_tboot |

### 1.1 The halted floor is 2.0 W, not 0.5 W

This is the result that moves the project most. Waking the Pi 4 from GPIO3 needs
`POWER_OFF_ON_HALT=0`, which leaves the board partly powered while halted. So halting saves
`P_idle − P_halt` = **1.26 W**, not the 2 W the plan assumed. That limit comes from the
architecture, not from how it was measured.

### 1.2 A boot costs about the same power as sitting idle

`P_boot − P_idle` is **0.01 W**, well inside the 0.2 W noise of the idle trace. So booting
adds almost nothing on top of the time it takes. `T_boot` is a **blind window**, not an
energy cost: 20.8 s during which a new event can't be seen.

`T_boot` splits into a 9.5 s firmware stage (wake → kernel), which the Pi's own `uptime`
never sees, and 11.3 s from kernel start to `# ready`. It is measured from the wake edge
on the power trace plus the daemon's `uptime`, never from the Pi's clock. The Pi has no
RTC, so after a wake its wall clock is only what `timesyncd` restored from disk.

Gate 0.4 (Sep 12, hand-jumper wake) measured 25.6 s instead, n=3, with a spread of only
0.2 s. The firmware stage is the same in both (9.35 vs 9.5 s). The whole 4.8 s difference
is in kernel → `# ready`, which dropped from 16.2 s to 11.3 s. Every Uno-caused boot since
(s12d, s18_tboot) gives 20.8 s. That is the configuration the matrix runs in, so it is the
number the model uses.

### 1.3 Measurement boundary: the case fans

The case fans run off the always-on 5 V rail. They draw **0.72 W halted** and **0.65 W
idle** (s11d, `data/fans/`), which is 36% of the halted floor. They cancel within 0.07 W in
every *difference* (break-even, `E_boot` net, `E_infer` net), but they inflate every
*absolute* figure. Without the fans the halted floor would be **1.27 W**. It is reported
alongside the measured one, not in place of it.

## 2. What the model predicts from these constants

`analysis/power_model.py --measured data/constants.json`. The closed form matches a
discrete-event simulation to 0.10% in power and 0.005 in detection rate. The predicted
Pareto front is straight to 1e-15 W.

**Break-even interval: effectively zero.** The exact form,
`T_boot·(P_boot − P_idle)/(P_idle − P_halt)`, gives **0.2 s**. The numerator is a 0.01 W
difference, so the honest reading is "indistinguishable from zero": **at any event rate a
person could produce, halting saves energy.** The naive renewal form (21 s) and the
first-order `E_boot/(P_idle − P_halt)` (54 s) both charge a boot as if it displaced
*halted* time. It displaces *idle* time, because the events it swallows would have kept
the Pi awake anyway.

**The cost is paid in detection, not energy.** With a 20.8 s boot and 15 s events, a Pi
that is halted when an event arrives can't see it. So the dormancy timeout is not an
energy optimisation: power is monotone in the timeout, and every second of it buys
detection rate at a fixed exchange rate, the Pareto slope `K`.

## 3. Pre-registered predictions for the overnight matrix

Registered Sep 18 before the run. Event duration 15 s (≈ 0.7·`T_boot`), exponential
arrivals, 40 events per cell, `tools/overnight.sh`. The model depends on the duration only
through whether an event outlives a boot, so every duration below `T_boot` gives the same
table (checked: `power_model.py --duration 10` and `--duration 15` print identical grids).

| Mean interval | Dormancy | Predicted P (W) | Predicted detection | Measured P | Measured detection |
|---|---|---|---|---|---|
| 20 s | 30 s | 3.035 | 0.631 | 2.817 | 0.625 |
| 20 s | never | 3.263 | 1.000 | 3.209 | 0.850 |
| 45 s | 30 s | 2.738 | 0.393 | 2.628 | 0.250 |
| 45 s | never | 3.261 | 1.000 | 3.192 | 0.875 |
| 20 s | 15 s | 2.864 | 0.354 | 2.667 | 0.200 |
| 45 s | 15 s | 2.583 | 0.213 | 2.494 | 0.125 |
| 20 s | 60 s | 3.203 | 0.903 | 2.997 | 0.575 |
| 45 s | 60 s | 2.965 | 0.656 | 2.864 | 0.450 |
| 120 s | 30 s | 2.395 | 0.195 | 2.383 | 0.225 |
| 120 s | never | 3.261 | 1.000 | 3.112 | 0.950 |

Measured Sep 19 (`data/overnight_0919_0226.log`, `data/overnight_0919_1204.log`).
Measured detection is the model's definition: events answered with the Pi already awake,
over the 40 shown. Two known departures from the model, to be analysed rather than fitted:
`event_display.py` draws each exponential gap *after* the previous image ends, so onsets
are 15 s + Exp(interval) apart rather than Exp(interval) (it matters least at 120 s, where
the cells agree best); and back-to-back images merge into one Tier 1 trigger, which caps
the never-halt cells below 1.000.

Pareto slope `K` (W per unit detection): **+0.614** at 20 s, **+0.861** at 45 s, **+1.075**
at 120 s. The falsifiable claim is that at one interval, the (detection, power) points for
every dormancy lie **on a straight line** with that slope. If the measured front curves,
one of the model's assumptions is wrong, and which one it is becomes the result.

With 40 events, a detection rate carries about ±0.08 of binomial noise at p≈0.5, so treat
single-cell deviations smaller than that as agreement.

## 4. Checking the predictions

**The Pareto front is straight, and its slope is the predicted `K`.** Fitting a line to the
(awake-detection, measured power) points at each interval:

| Mean interval | Predicted `K` | Fitted `K` | Ratio | r² | n |
|---|---|---|---|---|---|
| 20 s | +0.614 | +0.776 | 1.26 | 0.801 | 4 |
| 45 s | +0.861 | +0.926 | 1.08 | **0.989** | 4 |
| 120 s | +1.075 | +1.006 | 0.94 | — | 2 |

At 45 s the front is straight to r² = 0.989 and the slope is within 8% of the number registered
before the run. At 120 s, with only two dormancy settings, the slope is within 6%. At 20 s the
front is visibly noisier (r² = 0.80) and 26% too steep.

**The 20 s miss has a known cause, and it is ours, not the model's.** `event_display.py` draws each
exponential gap *after* the previous image finishes, so onsets are 15 s + Exp(interval) apart, not
Exp(interval). At a nominal 20 s interval that is a 75% inflation of the true mean gap; at 120 s it
is 12%. The cells therefore halt more often than the model was told they would, and the error runs
in exactly the direction the ranking of r² shows: worst at 20 s, best at 120 s. The model is not
falsified here — it was fed the wrong arrival process. A post-hoc re-run with the as-run process is
the obvious correction and is not yet done.

**Break-even was predicted at ≈ 0 s and behaves that way.** Every halting cell drew less average
power than its never-halting partner at the same interval, at every interval tested, down to 20 s.
No crossover was observed, as predicted.

### 4.1 What the model did not predict

The model reasons about *detection* — was the Pi awake to see the event. It has no notion of whether
the event was **still there** once the Pi finished booting. It is:

**`T_boot` (20.8 s) is longer than an image is shown (15 s).** A Pi woken by an event finishes
booting after the image it was woken for has already left the screen, and photographs a blank
monitor. The event counts as detected, answered, and classified — wrongly.

| Cells | End-to-end top-1 | Against a 0.700 ceiling |
|---|---|---|
| never-halt (3 cells) | 0.525, 0.550, 0.550 | 75–79% of ceiling |
| halting (7 cells) | 0.025 – 0.400 | 4–57% of ceiling |

Among the halting cells the ordering is monotone in how *little* they halt: 60 s dormancy keeps
0.275–0.400, 15 s dormancy collapses to 0.025–0.125. This is not classifier noise. It is the boot
time and the stimulus duration interacting, and it is the most important result of the project:

> **Dormancy buys up to 27% of Pi-rail energy and costs up to 95% of end-to-end accuracy, because
> the Pi finishes booting after the thing it woke for is gone.**

A tiered wake-up system whose slow tier boots slower than its stimulus lasts does not have an energy
/ accuracy trade-off worth making. The fix is not a better classifier; it is either a stimulus that
persists past `T_boot`, or a Tier 3 that resumes in well under 20.8 s (suspend-to-RAM rather than
halt), or a Tier 2 that buffers a frame for the Pi to classify on waking. None of the three was in
scope for this build, and naming that is the honest conclusion.

## 5. The four numbers

Per the project question, measured across the 10 matrix cells.

| | Best cell | Range across halting cells |
|---|---|---|
| **Energy saved** vs an always-on Pi (3.26 W idle) | **26.9%** (120 s / 30 s) | 8.1 – 26.9% |
| **Inferences avoided** vs always-on at 1 fps | **99.4%** | 97.4 – 99.4% |
| **Events missed** (not answered while awake) | 0.05 (120 s / never) | 0.05 – 0.875 |
| **Accuracy lost** vs the 0.700 ceiling | 0.15 (never-halt) | 0.30 – 0.675 |

"Inferences avoided" is close to meaningless as a headline: an always-on 1 fps baseline is a straw
man, since nothing in this system would ever run the classifier at 1 fps. The per-event baseline in
`accuracy.json` (`always_on_one_frame_per_event_duration`) gives 62% avoided and is the fairer
comparison. Quote that one.

## 6. Framing the savings honestly

**Every cell figure here is the Pi rail only.** The FNB58 sat between the wall brick and the Pi, so
Tiers 1 and 2 are outside every number in §2 and §3. They were metered separately on Sep 19 (§5 of
`docs/trigger_characterization.md`): Tier 1 draws **12 mW** watching, and the Uno board with Tier 1
on its 5 V pin draws **167 mW awake, 89 mW in power-down sleep** — the sleep step visible on the
meter 52 s after power-up, where the firmware's dormancy timer puts it.

**The cascade is net-positive in energy, but not by the margin the tiering story implies.** Weighted
by each cell's halted fraction, Tiers 1+2 cost about **0.11 W** against Pi-rail savings of 0.21 W to
0.73 W. So the overhead is ~15% of the best cell's saving (120 s / 30 s) and ~61% of the worst's
(20 s / 60 s). Note the direction: the cascade is *cheapest* relative to the saving at long
intervals, because rare events let the Pi halt more — the opposite of what we guessed before
measuring. Nothing here is net-negative, but a cascade that eats 61% of the benefit in its worst
configuration is a much weaker claim than "Tier 2 is free," and the 89 mW sleep floor is dominated
by the Uno's power LED and ATmega16U2 USB bridge, neither of which a deployed design would carry.

**The halted floor is 2.0 W, not zero.** `POWER_OFF_ON_HALT=0` is required for GPIO3 wake, so a
halted Pi keeps its always-on rail energised. Of that floor, 0.72 W is the case fans, which ran
throughout and are a measurement-boundary term, not a property of the design. The fanless halted
floor is **1.27 W**.

**Projected, clearly labelled as projected:** the best cell spends 75.7% of its wall time in the
halted state. A Tier 3 that could be power-gated to ~0 W for that time, with the same 20.8 s boot,
would go from 26.9% saved to **73.3%** — or **56.4%** if the case fans keep running, since they sit
on the always-on rail and gating the Pi does not switch them off. Both figures are arithmetic on the
measured halted fraction, not measurements: no power-gated configuration was built, and gating the
Pi's rail would break the GPIO3 wake this design depends on.

**Single night, n = 1 per cell.** The Sep 13–17 window was lost to a loose camera ribbon, so every
cell is one 45-minute run with 40 events. Detection rates carry ±0.08 of binomial noise. No cell was
repeated, so there is no run-to-run variance estimate anywhere in this report.

**One cell's classification is scored by index order.** The 20 s / 30 s cell has no usable
`arduino_t_ms` fit, so `accuracy.py` fell back to pairing results with stimuli in index order and
warns that results after a miss may be misattributed. Its 0.075 end-to-end figure should be treated
as indicative only. The other nine cells fitted the clock per sleep epoch.

**No ROC, so no defensible operating point.** The Tier 1 threshold was bracketed by hand, not chosen
from a sensitivity/false-trigger curve. The report cannot claim the trigger sits at a justified point
on a ROC, because the sweep was cut with the rest of Sep 13–17.

### Reproducing the figures

```sh
.venv/bin/python analysis/plots.py data/ --tag overnight          # pareto.png
.venv/bin/python analysis/plots.py \
    data/20260919T180457Z_i120_d15000_t30000_c0.8_int8_overnight \
    --only trace                                                  # trace.png
```

`--tag` is required: without it the eight Sep 19 rehearsals land in the Pareto alongside the matrix
and the front zig-zags between two populations. The trace is generated from a named cell rather than
the tag, because the tag's newest run is a never-halt cell with no boots in it — a flat line, and the
one thing the trace exists to show is the halt/boot staircase.
