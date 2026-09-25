# Experiments — matrix and run log

**Log every run here as it happens.** Condition, timestamp, CSV filename, and anything that went
wrong. A run you cannot identify later is a run you did not do.

`run_experiment.py` appends a skeleton row automatically; the *Notes* column is filled by hand.

---

## Before the matrix: Sep 7 blocking measurements

These four gate everything downstream. Nothing in §2 is worth running until they are done and
recorded here.

| # | Measurement | Command | Pass condition | Result |
|---|---|---|---|---|
| 0.1 | **Supply sanity** | `vcgencmd get_throttled` under sustained 100% CPU + camera streaming | **`throttled=0x0`.** Any nonzero under-voltage bit and every power number after it is garbage. | **PASS (CPU + camera), Sep 11** — `0x0` held through 45 s of 4-core load (ARM pinned 1.8 GHz, 34→47 °C) and through the 200x2 camera+inference bench (34.5→40.4 °C). An earlier cable gave a sustained `0x50005` (under-voltage flapping every ~10 s at 32 °C); swapping it fixed it. **Meter-in-line half PASS, Sep 12** — FNB58 in series, `0x0` before and after the whole 43.5 min boot-cycle trace and before and after the 3000-inference bench (35→43.8 °C). |
| 0.2 | **Board revision** | `grep Revision /proc/cpuinfo` | Note rev. `...111` = **rev 1.1**, which has the USB-C CC-resistor bug and refuses e-marked C-to-C cables. `...112` = rev 1.2, fine. The FNB58 sits in that chain. | **PASS**, Sep 11 — Rev 1.5, `a03115`. Not affected by the rev 1.1 cable bug. |
| 0.3 | **Inference + end-to-end latency** | `tools/latency_bench.py --compare -n 200` (add `--model-only` if no camera) | A number, whatever it is. If inference is far slower than assumed, the tier boundary moves — and it is better to learn that on day 1. | **PASS, Sep 11** — see the table below. `data/latency.json` on the Pi. imx477, 224x224 RGB888 stream, 4 threads, `throttled=0x0` throughout. |
| 0.4 | **Boot cost `E_boot`** | `analysis/energy_analysis.py --boot-cycle data/boot/` | **The single most important number in the project.** The ~100 J estimate is the basis of the entire break-even argument. Measure it, do not assume it. | **PASS, Sep 12** — `E_boot` **88.3 J** wall, **37.1 J** net of the halted floor, n=3, `T_boot` **25.6 s**. See 0.4 results below. |

#### 0.3 results — Pi 4 Rev 1.5, trixie, ai-edge-litert 2.2.0, 4 threads, n=200

| Model | inference p50 | inference p95 | end-to-end p50 | end-to-end p95 | capture p50 |
|---|---|---|---|---|---|
| **INT8** `mobilenet_v2_1.0_224_quant` | **18.39 ms** | **19.77 ms** | 20.70 ms | 22.35 ms | 1.06 ms |
| **FP32** `mobilenet_v2_1.0_224` | **40.60 ms** | **43.45 ms** | 43.66 ms | 46.39 ms | 1.05 ms |

INT8 is **2.21x** faster than FP32 at p50. Capture is ~1 ms and is not a factor at
either model's scale.

> **The ~100 ms inference assumption in `power_model.py` is wrong by ~5x.** Measured
> INT8 end-to-end is **20.7 ms**, not ~100 ms. `latency_awake` must be re-set from this
> before any break-even is quoted, and the tier boundary moves accordingly — that is
> s10k's job, and it is now a real correction rather than a formality.

> **Measure this on a supply you have verified.** The first attempt ran at a sustained
> `throttled=0x50005` and reported INT8 p50 = 52.5 ms and a 1.37x INT8/FP32 ratio —
> 2.9x slow, with the ratio compressed. `latency_bench.py` warned, but only because it
> checks `get_throttled` first; the numbers themselves looked perfectly plausible.

Record `P_idle`, `P_halt`, `P_boot`, `T_boot` here as soon as 0.4 is done, then re-run
`analysis/power_model.py --measured` so every prediction below uses real numbers.

| Constant | Estimate | Measured | Source |
|---|---|---|---|
| `P_idle` | 2.5 W | **3.26 W** | daemon idle, camera initialised. Reproduced in a second session at 3.23 W |
| `P_halt` | 0.5 W | **1.997 W** | after `sudo halt`, `WAKE_ON_GPIO=1`. **4x the estimate**; 1874 s over 3 windows |
| `P_boot` | 3.5 W | **3.45 W** | mean over the boot window, n=3 (3.44-3.45) |
| `T_boot` | 30 s | **25.6 s** | wake asserted → daemon prints `# ready`, n=3 (25.45-25.67). Includes a 9.4 s firmware stage |
| `E_boot` | ~100 J | **88.3 J** | integral over the boot window. **37.1 J net of `P_halt`** |
| `E_infer` (INT8) | 0.25 J | **61.8 mJ** | per-event, net of idle. **4x below the estimate**. 130.7 mJ wall |

#### 0.4 results — Pi 4 Rev 1.5, FNB58 in line, 100.0 Hz, `throttled=0x0` throughout

Three halt → GPIO3 wake → `# ready` cycles in one continuous 43.5 min trace
(`data/boot/`), plus a separate sustained-inference session (`data/infer/`). The
wake was a hand jumper, pin 5 → pin 20, since Tier 2 is not wired yet.

| | cycle 1 | cycle 2 | cycle 3 | **mean** |
|---|---|---|---|---|
| firmware stage (wake → kernel) | 9.35 s | 9.34 s | 9.37 s | **9.35 s** |
| `T_boot` (wake → `# ready`) | 25.45 s | 25.64 s | 25.67 s | **25.59 s** |
| `P_boot` | 3.451 W | 3.440 W | 3.450 W | **3.45 W** |
| `E_boot` wall | 87.8 J | 88.2 J | 88.5 J | **88.2 J** |
| `E_boot` net of `P_halt` | 37.0 J | 37.0 J | 37.3 J | **37.1 J** |

Two independent routes agree: explicit-window integration gave 88.2 J / 25.59 s, and
`energy_analysis.py --boot-cycle` gave 88.5 J / 25.7 s. Trapezoid vs the meter's own
counter: 6168.2 J vs 6168.2 J, 0.00% apart.

> **The halted floor is 2.0 W, not 0.5 W.** This is the finding that moves the
> project. `P_idle − P_halt` is **1.26 W**, not the assumed 2.0 W, so halting saves
> far less than the model assumed. It is architectural, not a measurement error:
> GPIO3 wake needs `POWER_OFF_ON_HALT=0`, which keeps the board partly powered.

> **The boot costs almost nothing above idle.** `P_boot − P_idle` is **0.19 W**, not
> 1.0 W. Re-running `power_model.py --measured` moves the exact break-even interval
> from **15.0 s to 3.9 s** — halting wins for any event rate slower than ~4 s. But
> the best-case saving (120 s interval) falls from **56% to 31%**. Halting pays off
> more often and pays less.

> **Sep 18 re-measure, Uno-caused wakes (`data/s18_tboot/`), supersedes the `T_boot`,
> `P_boot` and `E_boot` rows above.** Three button → Uno → GPIO3 wakes: `T_boot`
> **20.8 s** (20.66–20.92), `P_boot` **3.27 W**, `E_boot` **68.1 J** wall, **26.6 J** net,
> firmware stage 9.54 s. A fourth wake was dropped: a second button press during that
> boot put it at 26.8 s. This matches s12d (20.8 s, 26.6 J). The firmware stage is the
> same as Gate 0.4, and the 4.8 s gap is all kernel → `# ready` (16.2 → 11.3 s uptime).
> `constants.json` now holds these values. With them `P_boot − P_idle` is 0.01 W, and the
> exact break-even is 0.2 s, i.e. zero within noise.

> **Do not measure `T_boot` from the Pi's clock.** The Pi 4 has no RTC and
> `tier3-daemon.service` deliberately starts before the network, so the wall clock on
> the daemon's `# ready` line is whatever `timesyncd` restored from disk — it read
> ~100 s *before* the wake that caused it. And `uptime` starts at the kernel, missing
> the 9.35 s firmware stage entirely: a Pi-side `T_boot` under-reports by 37%. `T_boot`
> is the wake edge on the power trace plus the daemon's `uptime` at `# ready`.

> **Fans are inside the measurement boundary.** The dual case fans run at constant
> speed off the 5 V rail, halted or not. They cancel exactly in every *difference* —
> so the break-even, `E_boot` net and `E_infer` net are fan-free — but they inflate
> every *absolute* figure, including `P_halt` and the % saving.
>
> Sized Sep 12 (s11d), one 18.8 min trace at 100 Hz, `data/fans/`:
>
> | state | fans on | fans off | fans |
> |---|---|---|---|
> | halted | 1.992 W | 1.271 W | **0.72 W** |
> | idle, daemon + camera | 3.242 W | 2.593 W | **0.65 W** |
>
> The fans are 36% of the halted floor. They cancel to within 0.07 W between idle
> and halted, inside the idle noise (sd 0.2 W), so the differences above stand. A
> fanless Pi's halted floor is 1.27 W, still 2.5x the 0.5 W estimate: the rest is
> the SoC with `POWER_OFF_ON_HALT=0`. Fans stay on for the matrix. The writeup
> reports the fanless floor next to the measured one.

**`E_infer`, INT8** — 3000 frames back to back with the camera, `data/infer/`.
Busy plateau 65 s against 64.6 s expected (3030 frames × 21.32 ms mean end-to-end),
so the window is the work. `P_busy` 6.13 W, `P_idle` 3.23 W, 46.9 inferences/s:

| | per inference |
|---|---|
| `E_infer` wall | 130.7 mJ |
| **`E_infer` net of idle** | **61.8 mJ** — the model's `e_infer`; estimate was 250 mJ |

Keeping the camera initialised costs **0.56 W** continuously (3.23 W vs 2.67 W with
the daemon stopped).

Constants are in `data/constants.json`; `analysis/power_model.py --measured
data/constants.json` uses them.

#### Tier 2 ↔ Pi integration — Sep 12 (s12b, s12c), Uno on 9 V, fans on, `throttled=0x0`

Wiring checked first with no multimeter (`firmware/wirecheck`, Uno ADC, 1.8 kΩ stand-in for
the Pi's pull-up): TX divider **3.25 V**, wake released **3.30 V**, asserted **0.52 V**.
The series resistor is **330 Ω, not 1 kΩ** — 1 kΩ against the Pi's 1.8 kΩ pull-up would
leave GPIO3 at 1.18 V asserted and never wake it (INTERFACE §2.1).

| check | result |
|---|---|
| wake pulse seen on awake Pi's GPIO3 | LOW for ~207 ms (65 of 18851 polls), firmware says 200 ms |
| serial, both directions | `SYNC` offset, `SET`/`CFG` DORMANCY, EVT → ACK → RES (52 ms, awake) |
| full cycle ×2: dormancy → `HALT` → halt → 20 s settle → Uno sleeps → button → boot → `# ready` → held EVT | **pass, pass.** `# ready` at uptime 11.4 s / 10.9 s; EVT → RES 42 / 39 ms, `state_at_evt=booted` |

Evidence: `data/s10j_boot/` on the Pi (`daemon.log`, `events.csv` rows 1–2, `boots.csv`).

> **`arduino_t_ms` freezes while Tier 2 sleeps.** The same two cycles show it: EVTs 286 s apart
> by the Uno were ≥ 409 s apart in real time. This breaks `accuracy.py`'s pairing in every
> cell that halts. Open item, INTERFACE §4.

**s12d, one metered event with a boot in it** — `data/s12d_metered/` (power.csv on the Mac,
logs copied from the Pi), FNB58 at 100.0 Hz, 306.6 s, trapezoid vs meter counter 0.00% apart.
Pi halted by hand → button → boot (not measured: its `# ready` is in the previous run's log) →
fresh run, 30 s dormancy → **clapperboard 1.99 s at 6.53 W** over 3.42 W idle → `HALT` →
button → metered boot → `EVT` → `RES` in 49 ms, `state_at_evt=booted`, `throttled=0x0`.

| metered boot | value |
|---|---|
| firmware stage (wake → kernel) | 9.54 s |
| `T_boot` (wake → `# ready`) | **20.8 s** (daemon uptime 11.3 s) |
| `E_boot` wall | 68.1 J |
| `E_boot` net of `P_halt` 1.990 W | **26.6 J** |

> **This boot is ~5 s faster than Gate 0.4** (25.6 s, 37.1 J net). The firmware stage is the
> same; the kernel-to-`# ready` part fell from ~16.2 s to 10.9–11.4 s across all three
> Uno-caused boots today (s12c ×2, s12d). n=1 metered, so not a new constant yet — but if it
> holds, break-even moves again. Re-measure with a `--boot-cycle` run before s10k is closed.

Two analysis gaps this run exposed:

1. **Fixed — `--boot-cycle` put the kernel start 4 s into the firmware stage.** With no
   firmware level from `find_levels` (this busier trace split the ~2.65 W plateau), the
   kernel threshold fell back to (halt + idle)/2 = 2.71 W, right on the plateau. It now reads
   the plateau off 1–4 s after each wake. Gate 0.4's trace still gives 88.5 J / 9.50 s;
   the synthetic round-trip still passes.
2. **Open — `find_clapperboard` misses a burn when the trace starts halted.** It takes its
   baseline from the first 2 s; from a 1.99 W floor the burn sits inside a 70 s
   above-threshold boot and is never isolated. Matrix runs start with the Pi awake, where
   this path works, but default-mode boot windows (`find_boot_windows`, level-based) also
   found nothing here — matrix `E_boot` should come from edge bracketing, not levels.

Firmware bugs found on the bench and fixed before this (`ce807d9`): EVT and wake now go at
`PERSIST`, not at trigger release (every event was up to 5 s late); `REFRACTORY` runs from the
last moment the trigger was seen asserted, so one EVT per assertion even with contact bounce.

#### Two bugs in `energy_analysis.py` this measurement exposed (fixed)

1. **`find_levels` rejected a 98-second plateau.** The halted run came back as one
   98.1 s run plus twelve sub-50 ms slivers where the transition ramps clipped the
   edge of the ±0.25 W band, and the median of those thirteen was 0.02 s — under the
   1 s dwell test. Transit slivers are now dropped before dwell is judged.
2. **`--boot-cycle` could not find a boot on real hardware.** It treated a boot as the
   power *state* entered on leaving halt. Measured, the boot is a 2.7 W firmware
   plateau and then a noisy 3.4–5.1 W phase whose mean sits 0.19 W from idle, so no
   level separates them; the window closed at the end of the firmware stage and
   reported `T_boot` = 8.5 s. `--boot-cycle` now brackets by edges (halt exit →
   kernel step + daemon uptime). State detection also smooths over 1 s first, since
   per-sample noise was wider than the band. The synthetic round-trip still recovers
   all seven constants, at both the estimated and the measured level spacing.

---

## The matrix

**Priority-ordered, so stopping early still leaves a complete result.** Run them in this order.

### 1 — Primary: dormancy policy → average power vs detection rate

The headline. **Do not cut this.**

Sweep `dormancy_ms` × `event_rate`. Event duration is **fixed at 25 s (25000 ms)**,
about 4.2 s longer than the measured `T_boot` of 20.8 s. A wake-triggering image should
still be visible after boot; events arriving during a boot can still be missed.
Produces a Pareto family, one curve per event rate.

| Axis | Values |
|---|---|
| `dormancy_ms` | 0, 5 000, 15 000, 30 000, 60 000, ∞ (never halt) |
| mean inter-event interval | 10 s, 20 s, 45 s, 120 s — **bracketing the predicted 15 s break-even** |
| `duration_ms` | fixed, 25000 (25 s) |
| `contrast` | fixed, 0.8 |
| events per cell | ≥ 40 |

> **No reflash per cell.** `run_experiment.py` passes `--dormancy-ms` to the daemon, which
> `SET`s it on Tier 2 over the serial link and records the `CFG` value Tier 2 reports as
> *in effect* into `manifest.json` as `dormancy_ms_verified`. That is the number to trust
> — `--dormancy-ms` is only what was asked for. If a run's manifest shows
> `dormancy_ms_verified: null`, the firmware did not acknowledge the `SET` and **that
> cell's dormancy is unverified**; re-run it rather than reporting it.

> **Arrival distribution must be exponential, not fixed.** With a fixed dwell the Pi is either always
> awake at an arrival or always halted at one, so detection rate collapses to a step function and
> there is no curve to plot. Memoryless arrivals give a smooth front — and they are what the
> closed-form model in `power_model.py` assumes, so the prediction and the measurement are
> comparable. Use `--dwell-dist exponential`. A single fixed-dwell cell is worth running as a
> sanity check on timing, and no more.

Estimated cost: 6 × 4 = 24 cells. At 40 events the 120 s column alone runs ~97 min per cell, so
**schedule this across Sep 14–15 and run the 10 s and 20 s columns first** — they are the fastest,
they bracket the predicted break-even, and they already show the sign flip in the Pareto slope.
Drop to 25 events on the 120 s column if time runs short; note it in the log rather than
silently shortening.

Re-run `analysis/power_model.py --measured data/constants.json --intervals 10 20 45 120` after
Sep 7 — the break-even moves with the measured constants, and the columns should bracket wherever
it lands, not wherever it was estimated to land.

### 2 — Secondary: event-duration sweep

Directly characterises the blind-window cost. Dormancy fixed at 0 (always halt) so every event pays
a boot; sweep `duration_ms` across `T_boot`.

| Axis | Values |
|---|---|
| `duration_ms` | 0.25, 0.5, 0.75, 1.0, 1.5, 2.0 × `T_boot` |
| dormancy | 0 |
| mean interval | 120 s |

Prediction: detection rate is a step at `duration ≈ T_boot`, smeared by boot-time variance. **The
width of that smear is the boot-time jitter**, which is a free extra result.

Note the model predicts detection saturates at `1/(1 + λ·T_boot)`, not at 1.0, even for events
that comfortably outlive a boot — the arrivals swallowed *during* each boot are lost regardless
of duration. At a 120 s mean interval that ceiling is 0.8. If the measured plateau sits at 1.0,
Tier 2 is buffering more than the one pending event the contract allows.

### 3 — Secondary: INT8 vs FP32

Same stimulus, same code path, one flag. The most on-theme measurement available for a *Chips* & AI
workshop — quantization as an energy lever — and it costs almost nothing.

| Axis | Values |
|---|---|
| `--model` | `int8`, `fp32` |
| dormancy | ∞ (never halt — isolate inference from boot) |
| mean interval | 15 s |

Report latency, **energy per inference net of idle**, and accuracy against
`tools/reference_predict.py`. `analysis/accuracy.py data/<run_id>/` does the scoring: camera
top-1 against that ceiling on the same images, and it adds a top-1 panel to the quantization
figure. Expect the race-to-idle result: INT8 may draw *more* instantaneous
power while using less energy per inference. Power alone cannot distinguish efficient from stalled.

### 4 — Secondary: Tier 1 ROC

Mostly collected during build week (`trigger_characterization.md`); folds in here as a
figure. Trimmer position × patch contrast → detection rate vs false-trigger rate.

Sub-threshold flicker events are injected as false-positive bait and logged in `gen.csv` with
`image_id=NONE`. A Tier 1 firing on one is a false positive.

### 5 — Tertiary: latency by Pi state

One afternoon. Wake-to-classification latency for awake and halted, ≥ 20 samples each. Report the
distribution, not just the mean — the halted case's variance is the boot-time jitter from §2.

---

## Run log

Historical rows retain the durations and run IDs recorded at acquisition. New runs use
the corrected **25000 ms (25 s)** default; earlier rows are not 25 s measurements.

| Run ID | Date | Exp | dormancy_ms | mean interval | duration_ms | contrast | model | N | Detect % | Avg P (W) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `20260918T190214Z_i20_d10000_t-1_c0.8_int8_harness` | 20260918 | | -1 | 20 s | 10000 | 0.8 | int8 | 6 | | |  |
| `20260918T194053Z_i20_d10000_t-1_c0.8_int8_harness` | 20260918 | rehearsal | -1 | 20 s | 10000 | 0.8 | int8 | 6 planned | n/a | | Aborted: operator confirmed no D2 presses. Zero events do not measure detection. Cleanup and exit dormancy -1 acknowledged; excluded from benchmark. |
| `20260918T194355Z_i20_d10000_t-1_c0.8_int8_harness_retest` | 20260918 | harness | -1 verified | 20 s | 10000 | 0.8 | int8 | 6 | 100 | 3.228 | D2: 6/6 matched, 0 misses/spurious; camera top-1 2/6 vs source 5/6; banana 2/2, no false fires. Fixed gains, Sidecar display 1, unchanged framing. 100.1 Hz power; clean release and exit dormancy -1; Tier 1 untested. |
| `20260919T034521Z_i20_d10000_t30000_c0.8_int8_rehearsal` | 20260919 | | 30000 | 20 s | 10000 | 0.8 | int8 | 8 | | |  |
| `20260919T040134Z_i20_d10000_t30000_c0.8_int8_rehearsal` | 20260919 | | 30000 | 20 s | 10000 | 0.8 | int8 | 8 | | |  |
| `20260919T044008Z_i20_d10000_t30000_c0.8_int8_rehearsal` | 20260919 | | 30000 | 20 s | 10000 | 0.8 | int8 | 8 | | |  |
| `20260919T061931Z_i20_d10000_t30000_c0.8_int8_rehearsal` | 20260919 | | 30000 | 20 s | 10000 | 0.8 | int8 | 8 | | |  |
| `20260919T062635Z_i20_d15000_t30000_c0.8_int8_overnight` | 20260919 | | 30000 | 20 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T065235Z_i20_d15000_t-1_c0.8_int8_overnight` | 20260919 | | -1 | 20 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T071825Z_i45_d15000_t30000_c0.8_int8_overnight` | 20260919 | | 30000 | 45 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T080348Z_i45_d15000_t-1_c0.8_int8_overnight` | 20260919 | | -1 | 45 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T084900Z_i20_d15000_t15000_c0.8_int8_overnight` | 20260919 | | 15000 | 20 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T160436Z_i45_d15000_t15000_c0.8_int8_overnight` | 20260919 | | 15000 | 45 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T165332Z_i20_d15000_t60000_c0.8_int8_overnight` | 20260919 | | 60000 | 20 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T171922Z_i45_d15000_t60000_c0.8_int8_overnight` | 20260919 | | 60000 | 45 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T180457Z_i120_d15000_t30000_c0.8_int8_overnight` | 20260919 | | 30000 | 120 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260919T194830Z_i120_d15000_t-1_c0.8_int8_overnight` | 20260919 | | -1 | 120 s | 15000 | 0.8 | int8 | 40 | | |  |
| `20260920T010043Z_i60_d15000_t15000_c0.8_int8_demoA` | 20260920 | | 15000 | 60 s | 15000 | 0.8 | int8 | 3 | | | Demo A cascade take, one continuous shot |
| `20260920T012133Z_i30_d15000_t-1_c0.8_int8_beat1` | 20260920 | | -1 | 30 s | 15000 | 0.8 | int8 | 2 | | | awake path, 42 ms, with t2rx logging |
| `20260920T012503Z_i60_d15000_t15000_c0.8_int8_demoA2` | 20260920 | | 15000 | 60 s | 15000 | 0.8 | int8 | 2 | | | beats 2+3: halt step then boot into expired stimulus |
| `20260920T053118Z_i60_d30000_t15000_c0.8_int8_control` | 20260920 | | 15000 | 60 s | 30000 | 0.8 | int8 | 2 | | | positive control: stimulus 30s > T_boot 20.8s |
| `20260920T062359Z_i60_d30000_t15000_c0.8_int8_control` | 20260920 | | 15000 | 60 s | 30000 | 0.8 | int8 | 2 | | | positive control: stimulus 30s > T_boot 20.8s |
| `20260922T021858Z_i45_d25000_t15000_c0.8_int8_demo_normal` | 20260922 | | 15000 | 45 s | 25000 | 0.8 | int8 | 3 | | |  |
| `20260922T024914Z_i30_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 30 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T030920Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T043849Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T044925Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T050445Z_i20_d30000_t30000_c0.8_int8_overnight` | 20260922 | | 30000 | 20 s | 30000 | 0.8 | int8 | 40 | | |  |
| `20260922T054343Z_i20_d30000_t-1_c0.8_int8_overnight` | 20260922 | | -1 | 20 s | 30000 | 0.8 | int8 | 40 | | |  |
| `20260922T061933Z_i45_d30000_t30000_c0.8_int8_overnight` | 20260922 | | 30000 | 45 s | 30000 | 0.8 | int8 | 40 | | |  |
| `20260922T071518Z_i45_d30000_t-1_c0.8_int8_overnight` | 20260922 | | -1 | 45 s | 30000 | 0.8 | int8 | 40 | | |  |
| `20260922T081027Z_i120_d30000_t30000_c0.8_int8_overnight` | 20260922 | | 30000 | 120 s | 30000 | 0.8 | int8 | 40 | | |  |
| `20260922T100409Z_i120_d30000_t-1_c0.8_int8_overnight` | 20260922 | | -1 | 120 s | 30000 | 0.8 | int8 | 40 | | |  |
| `20260922T174931Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T175352Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T175844Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T180251Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T181350Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T181757Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T182204Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T182640Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |
| `20260922T183047Z_i20_d30000_t15000_c0.8_int8_demo_sleep_normal` | 20260922 | | 15000 | 20 s | 30000 | 0.8 | int8 | 4 | | |  |

---

## Checklist, every run

1. Monitor at **100% brightness**. Room lighting **identical** to the previous run — note any change.
2. `vcgencmd get_throttled` → `0x0` **before** and **after**. Record both.
3. `vcgencmd measure_temp` before and after. A throttled run measures the cooling, not the workload.
4. FNB58 on its own mains brick, `diskutil unmount "/Volumes/NO NAME"`, PC cable seated.
5. Clapperboard fires at t=0 — **confirm the 2 s step is visible in the trace before trusting the run.**
6. `RES` count vs `GEN` count reconciled. Unexplained gaps get investigated, not averaged away.
7. Manifest written, and `dormancy_ms_verified` matches the intended cell. **A run without
   a manifest is a run that did not happen.**
8. Tier 1's trimmer has not moved since the ROC sweep. If it has, note it — the cells
   before and after are not comparable.

## Sep 20 correction to the Sep 19 overnight classifications

A manual review of a recording from another device supplied 400 stimulus outcomes in
`data/overnight_manual_review.csv` for the ten `_overnight` runs. It confirmed that
the file's former synthetic labels came from LLM formatting and did not describe the data.
Those fields are now named `reviewed_class_id` and `reviewed_correct`.

Per-run `reviewed_events.csv` files supersede machine-derived classification outcomes in
`accuracy.json`, including on subsequent analysis runs. The corrected total is **189/400
(47.25%)**; per-cell results are in [REPORT.md §4.1](REPORT.md#41-what-the-model-did-not-predict).
All 400 stimulus references and 327 linked result references matched the original files.
The review subsequently confirmed that the first and final stimulus in every cell was
classified incorrectly, with its predicted label unknown. All 20 endpoint records carry
the marker `unknown` and correctness `0`. The review contains 27 additional outcomes
without matched machine rows, so it does not
provide a complete corrected timing, boot-state, firing or inference-count record.

Original inputs, reports and batch logs are backed up under `data/manual_review_originals/`.
Machine event, daemon and power records remain intact. Batch logs carry correction notices;
manifests identify the review source and hash. Historical duration remains 15 s; this review
does not establish performance for the current 25 s configuration.
