# Tiered Wake-Up: how much energy does an always-on camera actually need?

A three-tier hierarchical wake-up cascade for edge vision, and — the actual deliverable — a
measurement of where the tier boundary should sit.

**Christopher Wai · Juan José Torres Urgiles — Purdue Chips & AI Hackathon, September 2026**

---

## The question

A camera that runs a neural network on every frame spends nearly all its energy on scenes where
nothing happened. The obvious fix is to keep the expensive stage asleep and wake it only when
something changes.

Sleeping is not free. Bringing a halted Raspberry Pi 4 back is a **full boot** — about 30 s and on
the order of 100 J — and the camera is **blind for every second of it**. Halting saves only ~2 W
against idling. So dormancy pays for itself only if the machine stays down long enough, and if it
stays down too long it misses events.

That is a genuine two-sided tradeoff between **average power** and **detection rate**, with a
break-even that is analytically predictable and that **moves with event rate**. Predicting it and
then measuring it is the point of this project.

## The cascade

Each tier only decides whether the next one needs to wake up.

| Tier | Hardware | Job | Power |
|---|---|---|---|
| **1** | Photosensor → op-amp filter chain → comparator, all discrete | Detect a *change* in incident light. No clock, no code. | ~5 mW, always on |
| **2** | Arduino Uno, INT0 + power-down sleep | Confirm the trigger persisted, reject one-off noise, decide when to wake and when to re-halt Tier 3 | µW asleep, ~20 mW awake |
| **3** | Raspberry Pi 4 + HQ camera, MobileNetV2 INT8 via TFLite | Capture a frame and classify it | ~2.5 W awake, ~0.5 W halted |

```
   light ──▶ [Tier 1: analog change detector] ──comparator──▶ [Tier 2: Uno]
                      ~5 mW, always on          D2/INT0        µW asleep
                                                                  │
                                            ┌─────────────────────┴──────────────────┐
                                     GPIO3 wake line                          UART 9600
                                     (open drain)                             EVT / ACK / RES
                                            └─────────────────────┬──────────────────┘
                                                                  ▼
                                                   [Tier 3: Pi 4 + HQ camera]
                                                    ~2.5 W awake, ~0.5 W halted
```

Tier 1 is purely analog: a high-pass to reject the ambient DC level, a gain stage, a 30–50 Hz
low-pass to kill mains and display flicker, and a comparator whose trimmer sets the threshold.
Tier 2 sleeps between interrupts, validates persistence before escalating, and gates Tier 3 over a
serial link plus an open-drain wake line — halting the Pi after a configurable period of silence.
**That timeout is the parameter the whole experiment turns on.**

### Pi states — the table the experiment turns on

| State | Power | Response to an event |
|---|---|---|
| Awake (daemon idle) | ~2.5 W | ~100 ms |
| Halted | ~0.5 W | full boot, ~30 s — **blind window** |

There is no intermediate state; the Pi 4 has no usable suspend-to-RAM. *These are pre-measurement
estimates; `E_boot`, `P_idle` and `P_halt` are measured on Sep 7 and this table is updated.*

## The benchmark

A script on a Mac drives a monitor the rig watches, which supplies both exact ground truth and a
controllable event rate.

```
black dwell (length sets the event rate)
  └─ flash: trigger patch + ImageNet image, held for the event duration
       └─ back to black
```

Two independent regions are on screen at once, and the separation is the point:

- **Trigger patch** — a plain luminance square in a corner, which Tier 1's photosensor is aimed at.
  Its contrast sweeps on its own, so Tier 1's ROC is not confounded by whether a given photo happens
  to be a dark night scene.
- **Image region** — what the HQ camera frames and Tier 3 classifies.

Tier 3 runs **stock MobileNetV2 INT8 on ImageNet-1k** — no training, no dataset collection — and
fires when it sees the target class, **banana**: in the standard label set *and* holdable, so the
showcase demo is the same code path as the benchmark. A standard workload makes joules-per-inference
comparable against published figures, and a real ~71%-top-1 task produces an actual
accuracy-versus-power curve instead of one pinned at 100%. Running the model offline on the same
images first (`tools/reference_predict.py`) separates model error from screen-capture degradation.

Sweepable: event rate, event duration, trigger-patch contrast, and deliberately sub-threshold
flicker injected as Tier 1 false-positive bait.

## The prediction, before any measurement

`analysis/power_model.py` is committed **before** the matrix runs. Events arrive as a Poisson
process of rate λ; the Pi halts after a dormancy timeout `T_d`. Solving by renewal-reward, with
`W = e^(λT_d) − 1` the events handled per awake phase, `B = λ·T_boot` the arrivals a boot swallows,
and `s = 1` if the stimulus outlives a boot:

```
cycle  = (1 + W)/λ + T_boot
energy = P_halt/λ + P_boot·T_boot + P_idle·W/λ + E_infer·(s + W)
detect = (s + W) / (1 + W + B)
```

Three consequences, all falsifiable:

1. **Break-even mean inter-event interval** — where halting stops paying:
   `1/λ* = T_boot·(P_boot − P_idle)/(P_idle − P_halt)` ≈ **15 s** at the estimated constants.
   Note `(P_boot − P_idle)`, not `(P_boot − P_halt)`: a boot displaces time the Pi would have spent
   *idle*, because the events it swallows would have kept it awake anyway. The naive renewal
   argument gives 45 s and the back-of-envelope `E_boot/(P_idle − P_halt)` gives 52 s; both are the
   sparse-event limit and both are wrong once `λ·T_boot` stops being small.
2. **The optimum is bang-bang.** Both power and detection are linear-fractional in `W`, hence
   monotone in `T_d`. The power-optimal dormancy timeout is **0 or ∞** — never an interior value.
   Intermediate timeouts are not a power optimisation; they are bought detection rate.
3. **The Pareto front is exactly a straight line.** Eliminating `W` between the two forms, the
   denominator cancels identically and power is affine in detection rate, with slope
   `K = [(P_idle − P_halt)/λ − T_boot·(P_boot − P_idle)] / [(1 − s)/λ + T_boot]`,
   constant in `T_d` and sign-flipping at the break-even. **A measured front that curves falsifies
   an assumption — and which one is the result.**

`--monte-carlo` runs a discrete-event simulation of the same system; the closed form tracks it to
0.5% and the front is straight to 9e-16 W. That check is not decoration — it is what caught the
naive model, which overstates average power by 60% at a 15 s mean interval.

## Repository

```
docs/     INTERFACE.md (frozen Tier2↔Tier3 contract) · TEAMMATE_BRIEF.md (Tier 1/2 spec)
          EXPERIMENTS.md (matrix + run log) · trigger_characterization.md
firmware/ tier2_firmware/          Arduino Uno — Juan
pi/       pi_daemon.py · classify.py · models/
tools/    event_display.py · reference_predict.py · fnb58_logger.py
          mock_arduino.py · mock_pi.py · run_experiment.py · latency_bench.py
analysis/ power_model.py · energy_analysis.py · plots.py
```

**Start with [`docs/INTERFACE.md`](docs/INTERFACE.md).** It is the frozen contract both halves are
written against, and it contains the two ways to destroy a Pi.

## Setup

```bash
# Mac (stimulus, power logging, analysis)
brew install hidapi
tools/setup_mac.sh                  # builds .venv from tools/requirements.txt

# Pi (Tier 3)
pi/models/fetch_models.sh           # MobileNetV2 INT8 + FP32 + labels
python3 -m venv --system-site-packages .venv   # picamera2 is a system package
.venv/bin/pip install -r pi/requirements.txt
```

## Reproducing a run

```bash
# one matrix cell, end to end, no manual steps
tools/run_experiment.py --mean-interval 30 --duration-ms 5000 \
                        --dormancy-ms 30000 --contrast 0.8 --n-events 40

# analysis
analysis/energy_analysis.py data/<run_id>/
analysis/plots.py data/            # Pareto family, ROC, energy breakdown
```

## Honest reporting

The halted Pi still draws ~0.5 W because `WAKE_ON_GPIO=1` requires `POWER_OFF_ON_HALT=0`; that caps
achievable savings and it is an architectural constraint, not a measurement error. Results are
reported **both** as measured end-to-end savings **and** as projected savings for a truly
power-gated Tier 3, clearly labeled. Board-level Tier 2 sleep current is ~20 mA regardless of
firmware because the Uno's power LED and USB-serial chip cannot be disabled in software; the
ATmega's own current is reported separately and the distinction is stated.
