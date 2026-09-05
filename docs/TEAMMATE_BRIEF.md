# Tier 0 + Tier 1 — build brief (Juan)

**Revised 2026-09-05. This supersedes every earlier copy of this brief.**
Three things changed and one deliverable was cancelled — see §0 first, then §1 onward is the spec.

Read alongside [`INTERFACE.md`](INTERFACE.md), which is the frozen contract between your half and
Chris's. Where the two disagree, `INTERFACE.md` wins.

---

## 0. What changed since the brief you have

### 0.1 Tier 0 is now a **visible-light change detector**, not an IR beam-break

The stimulus is no longer a physical object breaking a beam. It is a **monitor** — a Mac script
flashes ImageNet photographs on a screen that the Pi's camera watches, which gives exact ground
truth, an arbitrary event rate, and a standard workload whose joules-per-inference can be compared
against published figures.

Nothing breaks an IR beam when the stimulus is a screen. So:

- **The 940 nm IR emitter is dropped.** No emitter at all now — the monitor is the light source.
- **Your sensor points at a "trigger patch"**: a plain white-on-black luminance square in a corner
  of the screen, separate from the image region the camera frames. Its contrast is swept
  independently, so your ROC curve is not confounded by whether a given photo happens to be dark.
- **The analog chain behind the sensor is unchanged.** High-pass → gain → low-pass → comparator with
  a trimmer threshold. That is ~90% of your work and none of it moves.

### 0.2 Check your phototransistor's package on **Sep 6**, not Sep 10

**PT204-6B is normally supplied in a black/blue IR-pass epoxy package.** If yours is dark, it is
IR-filtered and will barely see an LCD, which emits essentially no IR. This blocks everything
downstream, so check it first.

Test, five minutes: 5 V → phototransistor → 10 kΩ to GND, meter the junction, wave a white
phone-screen at it. A clear-package part will swing hundreds of millivolts. A dark-package part
will barely move while still reacting strongly to a TV remote pointed at it.

Fallbacks, in order of preference, both in the kit:
1. **Any clear-package phototransistor.**
2. **A photoresistor (LDR/CdS cell).** Its ~10–50 ms response sounds slow but the low-pass already
   sits at 30–50 Hz, so it costs nothing that matters here. Wire it as the top leg of a divider:
   5 V → LDR → node → 10 kΩ → GND. More light ⇒ higher node voltage, same polarity as the
   phototransistor, so the rest of the chain is untouched.

### 0.3 `event_generator.ino` is **cancelled**

The monitor is the event generator now. The second Arduino, the five colored LEDs and the diffuser
are all out. **Do not build it.** Your scope is Tier 0 and Tier 1, nothing else — this is a
reduction in your workload, not a reshuffle.

### 0.4 Two things about running against a screen

- **Shroud the sensor.** A short opaque tube (rolled black paper, heat-shrink, a pen barrel) aimed
  at the trigger patch. Without it, room light swamps the monitor and the SNR collapses. This is
  the difference between the circuit working and the circuit appearing broken.
- **Monitor at 100% brightness during every run.** LCDs dim by PWM-ing the backlight, typically at
  a few hundred Hz to a few kHz, and that is a noise source sitting right where your signal is.
  At 100% the backlight is usually DC. Verify on the scope on Sep 7.

---

## 1. What Tier 0 has to do

Assert a digital line when incident light **changes** by more than an adjustable threshold, using no
clock and no code, at roughly 5 mW. It must fire on a trigger-patch flash and **not** fire on room
lighting, on someone walking past, or on the monitor's own refresh.

It does not need to be smart. Tier 1 rejects one-off noise; Tier 0 only has to be cheap, always on,
and adjustable.

```
                            ┌──────── 5 V
                            │
  ┌────────┐            [ phototransistor ]                     ┌── to Arduino A0 (peak)
  │ monitor│  light         │                                    │
  │ patch  │ ~~~~~~~~▶  ────┤ ● V_photo                          │
  └────────┘            [ 10 kΩ ]                                │
                            │                          ┌─────────┴──────────┐
                           GND                         │                    │
                                                                            │
   V_photo ──┤├──────┬─────▶ ╲                    ┌───[33 kΩ]───┬───────────▶ LM339 IN−
            1 µF     │        ╲  LM358             │            │
                  [ 1 MΩ ]     ╲___ gain ×11 ──────┘         [0.1 µF]
                     │         ╱   (non-inv,                    │
                    2.5 V     ╱     ref 2.5 V)                  GND
                    bias     ╱
                                                    3386P trimmer wiper ────▶ LM339 IN+
                                                                                  │
                                    5 V ──[10 kΩ]──┬──── LM339 OUT ───────────────┘
                                                   │      (open collector)
                                                   └────────▶ Arduino D2 (INT0)
                                                              LOW = event
```

### Stage by stage

**1 — Photosensor.** As drawn, or the LDR divider from §0.2. Size the 10 kΩ so the quiescent output
under normal room light lands around **1–2 V**, leaving headroom in both directions. Easiest way:
put a 100 kΩ trimmer there during bring-up, tune it, meter it, then solder in the nearest fixed
value.

**2 — High-pass, 1 µF / 1 MΩ biased to 2.5 V.** f_c = 1/(2π·10⁶·10⁻⁶) ≈ **0.16 Hz**. Strips the DC
ambient level so the threshold is about *change*, not absolute brightness — which is the whole
point, and why the circuit tolerates a room getting gradually brighter.

Build the 2.5 V rail from two 10 kΩ resistors across 5 V with a **10 µF bypass to ground**. Without
that bypass the "reference" moves with the signal and the stage does nothing useful.

*If slow ambient drift turns out to be a problem, drop the 1 MΩ to 100 kΩ (f_c ≈ 1.6 Hz). The cost
is droop on long flashes, which is acceptable because the comparator fires on the onset edge.*

**3 — Gain, LM358N non-inverting, referenced to 2.5 V.** Start at **×11** (Rf = 100 kΩ, Rg = 10 kΩ).
Single-supply, so the output swings roughly 0 V to VCC−1.5 V; sitting at 2.5 V leaves about ±1.5 V
of usable range. If the comparator needs the trimmer at an extreme end of its travel, the gain is
wrong — fix the gain, not the trimmer.

*Check the kit for the actual part. An LM324 is a drop-in. A 741 is not — it needs split supplies.*

**4 — Low-pass, 33 kΩ / 0.1 µF ≈ 48 Hz.** Kills mains flicker at 120 Hz and most backlight PWM.

Be aware a single pole gives only ~−8 dB at 120 Hz. **If the false-trigger rate is stubborn, cascade
a second identical RC.** That is the first thing to try, before touching the threshold, because
lowering sensitivity to fix a noise problem loses you real detections too.

**5 — Comparator, LM339N + 3386P trimmer.** Signal to **IN−**, trimmer wiper to **IN+**. So a rising
light step drives the output **LOW**, which is what you want: it is the active direction of an
open-collector output, and it feeds the Uno's level-triggered INT0 wake directly.

> **The LM339's output is open collector — it cannot drive high.** It needs a **10 kΩ pull-up to
> 5 V** or it looks completely dead. This is by far the most common way this circuit appears broken
> when it is fine.

**Add hysteresis:** 1 MΩ from OUT back to IN+. With the trimmer's ~2.5 kΩ source impedance that is
~12 mV of hysteresis — enough to stop the comparator chattering on slow edges. Without it, one
event can generate a burst of interrupts and Tier 1 will see phantom events.

**6 — A0 tap.** Take Arduino A0 from the low-pass output (stage 4), *before* the comparator, so the
firmware can report `peak` in each `EVT`. Idle sits near 2.5 V ⇒ ADC ≈ 512.

**7 — An LED on the comparator output.** LED + 1 kΩ from 5 V to the output (it lights when the
output goes low). Do this early. Being able to *see* the trigger fire, with no Arduino in the
circuit at all, is what makes stage-by-stage debugging fast.

---

## 2. What Tier 1 has to do

An Arduino Uno that validates triggers, gates the Pi, and gets out of the way.

Pins, matching `INTERFACE.md` §6:

| Pin | Role |
|---|---|
| D0 / D1 | Serial to the Pi, 9600 8N1. **D1 goes through the divider.** |
| D2 | Comparator output. INT0. **LOW = trigger asserted.** |
| D7 | Wake line → 1 kΩ → Pi GPIO3. **Open-drain only.** |
| A0 | Pre-comparator analog, for `peak` |

### State machine

```
        ┌─────────────────────────────────────────────┐
        │                                             │
        ▼                                             │
   PI_AWAKE ──── dormancy timer expires ────▶ send HALT
   (Uno idles,                                        │
    millis() runs,                            wait HALT_SETTLE_MS
    dormancy timer runs)                              │
        ▲                                             ▼
        │                                        PI_HALTED
        │                                     (Uno sleeps in
   daemon sends "# ready" ◀── assert wake ◀──   PWR_DOWN until
                                                INT0 goes LOW)
```

### The one thing that will silently break the experiment

**`millis()` does not advance in `SLEEP_MODE_PWR_DOWN`.** Timer0 is stopped along with everything
else. A dormancy timer built on `millis()` while the Uno sleeps will never expire, or will expire at
an arbitrary time — and the dormancy timeout is the swept variable of the headline experiment, so
that failure would quietly corrupt the main result rather than announce itself.

The fix is a design choice, not a workaround: **only sleep while the Pi is halted.**

- **Pi awake** → the Uno stays awake and idles. `millis()` runs, the dormancy timer is exact. The
  Uno's ~20 mW next to the Pi's 2500 mW is nothing; there is no reason to sleep here.
- **Pi halted** → the Uno enters `SLEEP_MODE_PWR_DOWN` and waits for INT0. **No timer is needed in
  this state at all** — the next event is the only thing that can happen.

This removes the need for a watchdog wake-and-count entirely, and it puts the deep sleep exactly
where it matters: in the state the system spends most of its time in when dormancy is working.

### Sequence, per event

1. **INT0 fires** (level-triggered `LOW` — power-down stops the I/O clock, so edge detection is
   unavailable and `LOW` is the only mode that wakes the part).
2. `detachInterrupt(0)` immediately, so the ISR does not re-fire while the line is still low.
3. **Persistence check.** Poll D2 for `PERSIST_MS` (start at 40 ms). Sample A0 throughout and keep
   the maximum excursion from 512 as `peak`. If the line releases early, it was noise: log
   `# noise`, re-arm, done. **This is Tier 1's entire reason to exist — do not skip it.**
4. **Wake the Pi if halted:** hold D7 low ≥ 200 ms, release, wait for `# ready` (up to
   `BOOT_TIMEOUT_MS`). Do not send `EVT` before then; nothing is listening.
5. **Send** `EVT,<millis()>,<peak>,<duration_ms>`.
6. Expect `ACK` within `ACK_TIMEOUT_MS`, then `RES` within `RES_TIMEOUT_MS`. Log a miss on either
   timeout and carry on — **never retransmit.** A lost event is data.
7. **Reset the dormancy timer.** Wait out `REFRACTORY_MS`, re-attach INT0.

### Power-down checklist

Before `sleep_mode()`:

```c
ADCSRA &= ~(1 << ADEN);       // ADC off first — it alone is ~250 µA
power_all_disable();          // then the rest of the peripherals
```

and re-enable both on wake, **ADC before the first `analogRead`** or `peak` reads as garbage.

**Be honest about the number you report.** Even done perfectly, an Uno *board* sits around 20 mA
asleep — the power LED and the USB-serial chip dominate and neither can be turned off in software.
Measure the **ATmega's own** current with the DMM in series with the VCC line, report that as the
silicon figure, and state plainly that a board-level µW result would need a bare ATmega on a custom
board. Judges reward the honest version; an unexplained µW claim next to a visibly lit Uno invites
the one question you cannot answer.

---

### Runtime-settable parameters — this saves you three days of being on call

**Added Sep 5. Read this before you write the constants as `#define`s.**

`DORMANCY_MS` is the swept variable of the headline experiment: six values × four event
rates = **24 matrix cells**, run across Sep 14–16. If it is a compile-time constant,
every one of those cells needs a reflash — which means *you personally have to be sitting
at the rig for three days*, and the run script can only record what we claim was flashed
rather than what the hardware actually had.

So the three swept constants are settable over the serial link, per
[`INTERFACE.md`](INTERFACE.md) §1.1:

```
Pi      → SET,DORMANCY,30000
Arduino → CFG,DORMANCY,30000        the value now IN EFFECT
Pi      → GET,DORMANCY
Arduino → CFG,DORMANCY,30000
```

| Key | Units | Range, clamp outside it | Default on reset |
|---|---|---|---|
| `DORMANCY` | ms | `0`–`3600000`, or **`-1` = never halt** | 30000 |
| `PERSIST` | ms | `0`–`5000` | 40 |
| `REFRACTORY` | ms | `0`–`60000` | 500 |

Four rules, and the first is the one that matters:

1. **`CFG` reports the value in effect, never the value requested.** Clamp an
   out-of-range value and return the *clamped* figure. `run_experiment.py` writes that
   number straight into the run manifest, so a clamp or a typo shows up in the record
   instead of silently mislabelling a matrix cell.
2. Unknown key → `# ERR unknown key <key>` and **no `CFG`**. The absence of a `CFG` is
   how the Pi detects firmware older than the key it asked for.
3. **RAM only — do not touch EEPROM.** The daemon sets all three at every run start, so
   persistence buys nothing, and 24 cells a day would start eating the write budget.
4. A `SET` arriving mid-event applies **after** that event finishes. Changing the
   refractory period underneath a running timer is a race for no benefit.

Keep them as ordinary `volatile`-free globals initialised to the defaults above, and the
firmware behaves exactly as this brief describes if nobody ever sends a `SET`.

A working reference implementation is in `tools/mock_arduino.py` (`handle_config`), and
`python3 tools/mock_arduino.py --self-test` exercises it — including the clamps and the
unknown-key case. Run it and read the output; that is the behaviour to match.

## 3. What to deliver, and when

| When | Deliverable | Done means |
|---|---|---|
| **Sep 6** | Phototransistor package checked | You know whether PT204-6B works or you are on the fallback. One message to Chris either way. |
| **Sep 7** | Sensor sees the monitor | Scope trace of the **raw, unfiltered** sensor output showing a clear step on a patch flash. If it does not, swap the sensor that day. |
| **Sep 8** | Analog chain complete | LED on the comparator fires on a patch flash and does **not** fire on room lighting. **Report the false-trigger rate over a 5-minute quiet window.** |
| **Sep 9** | `tools/mock_pi.py` + `firmware/tier1_firmware/tier1_firmware.ino` | Firmware runs the full state machine against the mock over USB, with no Pi. `EVT` lines are well-formed, `SET`/`GET` round-trip with clamping, and the Uno measurably drops to µA between events. |
| **Sep 10** | Integration, in person | §2.5 of `INTERFACE.md` wiring checklist **meter-verified before anything touches GPIO3**, then SYNC → EVT → RES → HALT → wake. |
| **Sep 13** | `docs/trigger_characterization.md` | Schematic with final values, the threshold × contrast sweep (≥ 6 trimmer positions), and Tier 0 / Tier 1 DMM current figures. |

### `tools/mock_pi.py` — what it has to do

It stands in for the Pi so the firmware is fully testable with nothing but a USB cable.
Mirror of `tools/mock_arduino.py`, which you can read for the shape.

- Connect to the Arduino's USB serial (`/dev/cu.usbmodem*` on a Mac) at 9600 8N1.
- Print `# ready` on connect — the firmware waits for exactly this before sending a
  pending `EVT` after a wake.
- Answer `EVT` with `ACK` immediately, then `RES,<class>,<conf>,<latency>` after a
  configurable delay (default ~100 ms, to imitate a real inference).
- Answer `SYNC,<t>` with `SYNC,<own ms>`.
- **Send `SET,DORMANCY,<ms>` on connect and check the `CFG` that comes back**, so the
  configuration path is exercised the same way the real daemon exercises it.
- Answer `HALT` with `ACK`, then **go silent for a configurable ~30 s** to imitate a
  halted-then-booting Pi, then print `# ready` again. This is the only way to test the
  boot path without a Pi, and the boot path is where the misses come from.
- Ignore every line starting with `#`, and drop malformed lines without wedging.
- Count and print: events sent, ACKs, RES sent, timeouts. A summary at exit.

A `--flaky` mode that drops one message in ten is worth twenty minutes — it is the
cheapest way to prove the firmware never blocks waiting for something that will not come.

Build the analog chain **one stage at a time and verify each before adding the next.** A four-stage
chain debugged only at its output is a bad afternoon; a chain verified stage by stage is a good
hour. Scope or meter after every stage.

## 4. After build week — Sep 14 to 22

Your brief used to stop at Sep 13. It should not: three of the four remaining
deliverables need something from you.

### Sep 14–16, the experiment matrix

With `SET,DORMANCY` implemented **you are not needed at the rig**, which is the point of
building it. What you *are* on the hook for:

- **Tier 0 must not drift between cells.** The matrix runs for three days and compares
  cells against each other; a trimmer knocked between Tuesday and Thursday silently
  invalidates the comparison. Lock the trimmer (nail varnish, hot glue, a dab of epoxy)
  once the ROC sweep is done, and **record the final wiper voltage** in
  `docs/trigger_characterization.md`. If it has to be re-adjusted mid-matrix, say so
  loudly — the affected runs get re-run, not quietly averaged in.
- Be reachable. A Tier 0 fault during a run looks exactly like a low detection rate,
  which is also the result we are trying to measure.

### Sep 13, the ROC data — in a specific format

`analysis/plots.py` draws your ROC curve straight from **`data/tier0_roc.csv`**. Write it
in exactly these columns or the figure will not build:

```csv
trimmer,contrast,false_per_min,detect_rate,n_events,notes
2.5,0.80,0.4,0.95,40,shroud fitted
2.5,0.40,0.4,0.62,40,
4.0,0.80,0.0,0.71,40,room lights off
```

- `trimmer` — turns from the CCW end, or the wiper voltage; whichever you use, be
  consistent, because it becomes the series label on the plot.
- `false_per_min` — from the quiet-window count, not estimated.
- `detect_rate` — detections ÷ `n_events`, 0–1.
- One row per (trimmer, contrast) cell. **≥ 6 trimmer positions.**

The prose write-up still goes in `docs/trigger_characterization.md`; this CSV is just the
machine-readable half of the same measurements.

### Sep 18, the video

The cascade firing is the shot that makes the architecture legible, and it is yours: the
comparator LED lighting, then the Uno waking, then the Pi's power trace stepping up. Have
the LED on the comparator output still fitted on Sep 18 — do not tidy it away after
debugging, it is the only visible evidence Tier 0 exists.

### Before Sep 19, transport

**Tier 0 on a breadboard will not survive being carried to the showcase.** Loose jumpers
in a four-stage analog chain are the single most likely way this rig arrives dead on
Sep 22.

- Mount Tier 0, the Uno and the Pi on **one rigid board** while everything is wired and
  working — not on Sep 21.
- Strain-relieve the sensor lead and the four wires crossing to the Pi (a zip tie to the
  board is enough).
- Photograph the working breadboard before moving anything, so a knocked wire can be put
  back from a picture rather than from memory.
- Bring the DMM and a spare of each passive you used.

## 5. When you are blocked

Message Chris. Specifically, message immediately if:

- the phototransistor turns out to be IR-filtered **and** the LDR fallback also cannot see the patch
- the false-trigger rate over a quiet window will not come down after cascading a second RC pole
- the Uno will not stay in power-down, or wakes spontaneously
- anything about the wiring in `INTERFACE.md` §2 is unclear — **ask before connecting, not after.**
  A 5 V slip on GPIO3 ends the project, and there is no spare Pi.

Everything Chris is building runs against mocks and needs no hardware from you, so a delay on your
side does not block him. Say so early and the schedule absorbs it.
