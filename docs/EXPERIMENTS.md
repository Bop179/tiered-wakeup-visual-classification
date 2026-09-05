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
| 0.1 | **Supply sanity** | `vcgencmd get_throttled` under sustained 100% CPU + camera streaming | **`throttled=0x0`.** Any nonzero under-voltage bit and every power number after it is garbage. | _pending_ |
| 0.2 | **Board revision** | `grep Revision /proc/cpuinfo` | Note rev. `...111` = **rev 1.1**, which has the USB-C CC-resistor bug and refuses e-marked C-to-C cables. `...112` = rev 1.2, fine. The FNB58 sits in that chain. | _pending_ |
| 0.3 | **Inference + end-to-end latency** | `tools/latency_bench.py --model int8 -n 200` | A number, whatever it is. If inference is far slower than assumed, the tier boundary moves — and it is better to learn that on day 1. | _pending_ |
| 0.4 | **Boot cost `E_boot`** | `analysis/energy_analysis.py --boot-cycle data/boot/` | **The single most important number in the project.** The ~100 J estimate is the basis of the entire break-even argument. Measure it, do not assume it. | _pending_ |

Record `P_idle`, `P_halt`, `P_boot`, `T_boot` here as soon as 0.4 is done, then re-run
`analysis/power_model.py --measured` so every prediction below uses real numbers.

| Constant | Estimate | Measured | Source |
|---|---|---|---|
| `P_idle` | 2.5 W | _pending_ | daemon idle, camera initialised |
| `P_halt` | 0.5 W | _pending_ | after `sudo halt`, `WAKE_ON_GPIO=1` |
| `P_boot` | 3.5 W | _pending_ | mean over the boot window |
| `T_boot` | 30 s | _pending_ | wake asserted → daemon prints `# ready` |
| `E_boot` | ~100 J | _pending_ | integral over the boot window |
| `E_infer` (INT8) | — | _pending_ | per-event, net of idle |

---

## The matrix

**Priority-ordered, so stopping early still leaves a complete result.** Run them in this order.

### 1 — Primary: dormancy policy → average power vs detection rate

The headline. **Do not cut this.**

Sweep `dormancy_ms` × `event_rate`. Event duration is **fixed** at a value where an awake Pi always
catches the event and a booting one sometimes does not — pick it from the measured `T_boot`, around
`0.5·T_boot`. Produces a Pareto family, one curve per event rate.

| Axis | Values |
|---|---|
| `dormancy_ms` | 0, 5 000, 15 000, 30 000, 60 000, ∞ (never halt) |
| mean inter-event interval | 10 s, 20 s, 45 s, 120 s — **bracketing the predicted 15 s break-even** |
| `duration_ms` | fixed, ≈ 0.5·`T_boot` |
| `contrast` | fixed, 0.8 |
| events per cell | ≥ 40 |

> **No reflash per cell.** `run_experiment.py` passes `--dormancy-ms` to the daemon, which
> `SET`s it on Tier 1 over the serial link and records the `CFG` value Tier 1 reports as
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

Estimated cost: 6 × 4 = 24 cells. At 40 events the 120 s column alone runs ~80 min per cell, so
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
Tier 1 is buffering more than the one pending event the contract allows.

### 3 — Secondary: INT8 vs FP32

Same stimulus, same code path, one flag. The most on-theme measurement available for a *Chips* & AI
workshop — quantization as an energy lever — and it costs almost nothing.

| Axis | Values |
|---|---|
| `--model` | `int8`, `fp32` |
| dormancy | ∞ (never halt — isolate inference from boot) |
| mean interval | 15 s |

Report latency, **energy per inference net of idle**, and accuracy against
`tools/reference_predict.py`. Expect the race-to-idle result: INT8 may draw *more* instantaneous
power while using less energy per inference. Power alone cannot distinguish efficient from stalled.

### 4 — Secondary: Tier 0 ROC

Mostly collected by Juan during build week (`trigger_characterization.md`); folds in here as a
figure. Trimmer position × patch contrast → detection rate vs false-trigger rate.

Sub-threshold flicker events are injected as false-positive bait and logged in `gen.csv` with
`image_id=NONE`. A Tier 0 firing on one is a false positive.

### 5 — Tertiary: latency by Pi state

One afternoon. Wake-to-classification latency for awake and halted, ≥ 20 samples each. Report the
distribution, not just the mean — the halted case's variance is the boot-time jitter from §2.

---

## Run log

| Run ID | Date | Exp | dormancy_ms | mean interval | duration_ms | contrast | model | N | Detect % | Avg P (W) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| _(first run appends here)_ | | | | | | | | | | | |

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
8. Tier 0's trimmer has not moved since the ROC sweep. If it has, note it — the cells
   before and after are not comparable.
