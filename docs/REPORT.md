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

**The cost is paid in detection, not energy.** With a 20.8 s boot and 10 s events, a Pi
that is halted when an event arrives can't see it. So the dormancy timeout is not an
energy optimisation: power is monotone in the timeout, and every second of it buys
detection rate at a fixed exchange rate, the Pareto slope `K`.

## 3. Pre-registered predictions for the overnight matrix

Registered Sep 18 before the run. Event duration 10 s (≈ 0.5·`T_boot`), exponential
arrivals, 40 events per cell, `tools/overnight.sh`.

| Mean interval | Dormancy | Predicted P (W) | Predicted detection | Measured P | Measured detection |
|---|---|---|---|---|---|
| 20 s | 30 s | 3.035 | 0.631 | | |
| 20 s | never | 3.263 | 1.000 | | |
| 45 s | 30 s | 2.738 | 0.393 | | |
| 45 s | never | 3.261 | 1.000 | | |
| 20 s | 15 s | 2.864 | 0.354 | | |
| 45 s | 15 s | 2.583 | 0.213 | | |
| 20 s | 60 s | 3.203 | 0.903 | | |
| 45 s | 60 s | 2.965 | 0.656 | | |
| 120 s | 30 s | 2.395 | 0.195 | | |
| 120 s | never | 3.261 | 1.000 | | |

Pareto slope `K` (W per unit detection): **+0.614** at 20 s, **+0.861** at 45 s, **+1.075**
at 120 s. The falsifiable claim is that at one interval, the (detection, power) points for
every dormancy lie **on a straight line** with that slope. If the measured front curves,
one of the model's assumptions is wrong, and which one it is becomes the result.

With 40 events, a detection rate carries about ±0.08 of binomial noise at p≈0.5, so treat
single-cell deviations smaller than that as agreement.
