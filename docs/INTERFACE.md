# INTERFACE — the contract between Tier 1 and Tier 2

**Status: frozen as of 2026-09-05.** Both halves are written against this document.
If something here has to change, it changes *here first*, in a commit, and the other
owner is told. Do not diverge locally.

Owners: **Tier 0 + Tier 1 (analog trigger, Arduino firmware) — Juan.**
**Tier 2 + instrumentation (Pi, daemon, power logging, analysis) — Chris.**

---

## 1. Serial link

| Property | Value |
|---|---|
| Physical | Arduino Uno hardware UART (D0 = RX, D1 = TX) ↔ Pi 4 GPIO14 (TXD, pin 8) / GPIO15 (RXD, pin 10) |
| Baud | **9600**, 8N1 |
| Line terminator | `\n` sent. A trailing `\r` is accepted and stripped on receive. |
| Encoding | ASCII, comma-separated, no spaces around commas |
| Max line length | 64 bytes including terminator. Longer lines are dropped. |
| Ground | **Common ground is mandatory.** Arduino GND ↔ Pi GND (pin 6, 9, 14, 20, 25, 30, 34 or 39). |

### Why the hardware UART and not SoftwareSerial

The Uno's hardware UART is also its USB serial. That is deliberate and it buys one thing:
with the Arduino plugged into a laptop by USB and **nothing wired to the Pi**, `tools/mock_pi.py`
speaks the exact same protocol over `/dev/cu.usbmodem*`. Juan can develop and verify the whole
firmware state machine with no Pi, no level shifting and no extra adapter, and the code path is
identical to the integrated one.

Consequence: **the USB cable must be unplugged from the Arduino before wiring D0/D1 to the Pi**,
and re-plugged to reflash. Both talkers on one RX pin will corrupt the link.

### Debug output shares the link

Because there is only one UART, debug printing and protocol traffic go down the same wire.

> **Any line beginning with `#` is a comment. Both sides must ignore it completely.**

So `Serial.println("# woke, peak=412");` is always safe. Protocol lines never start with `#`.

### Messages

| Direction | Message | Meaning |
|---|---|---|
| Arduino → Pi | `EVT,<t_ms>,<peak>,<duration_ms>` | A validated Tier 0 event. |
| Arduino → Pi | `HALT` | Dormancy timeout expired. Pi should halt. |
| Pi → Arduino | `ACK` | Last message received and accepted. |
| Pi → Arduino | `RES,<class_id>,<confidence>,<latency_ms>` | Classification result for the most recent `EVT`. |
| Either → other | `SYNC,<t_ms>` | Clock reconciliation. See §4. |
| Either → other | `# <anything>` | Comment. Ignore. |

**Fields**

- `t_ms` — sender's own `millis()` / monotonic milliseconds. Unsigned integer. **Wraps at 2^32 ms ≈ 49.7 days; nobody needs to handle the wrap.** Never treat this as wall-clock.
- `peak` — Tier 1's ADC reading at the event peak, `0..1023`. Integer. Recorded for the Tier 0 ROC; the Pi does not act on it.
- `duration_ms` — how long the trigger stayed asserted before Tier 1 accepted it. Integer.
- `class_id` — integer index into `pi/models/labels.txt`. **`-1` means "no confident classification"** (below `--conf-threshold`).
- `confidence` — float, `0.000`–`1.000`, three decimals.
- `latency_ms` — integer, measured on the Pi from `EVT` line received to result ready. Includes capture.

### Rules

1. **Every `EVT` gets exactly one `ACK`,** sent by the Pi as soon as the line is parsed — *before*
   capture, not after. `ACK` means "I am awake and I heard you", nothing more.
   **An `EVT` whose fields will not parse is not acked at all** (rule 4 takes over). An `ACK`
   promises a `RES`; acking a line that can never produce one would leave Tier 1 waiting out
   `RES_TIMEOUT_MS` and would count as a miss for the wrong reason.
2. `RES` follows its `ACK` by `latency_ms`. If the Pi cannot classify (camera error, timeout) it
   still sends `RES,-1,0.000,<latency_ms>` so counts stay aligned. **Never stay silent.**
3. **`HALT` gets an `ACK`, then a `# halting` comment, then the Pi runs `sudo halt`.** See §3.
4. **Malformed line → drop it silently.** Do not block, do not retry, do not reset. Optionally emit
   `# ERR <first 20 chars>`. A malformed line must never wedge either state machine.
5. **Never block on a read.** The Arduino must remain able to sleep; the Pi must remain able to
   service the camera. Both sides poll.
6. **The Pi is stateless about event numbering on the wire.** It keeps its own monotonically
   increasing `event_idx` in its log (§5). The Arduino never sends an index.

### Timeouts

| Constant | Value | Who | Meaning |
|---|---|---|---|
| `ACK_TIMEOUT_MS` | 2000 | Arduino | No `ACK` within 2 s of `EVT` ⇒ Pi is halted or booting. |
| `RES_TIMEOUT_MS` | 5000 | Arduino | No `RES` within 5 s of `ACK` ⇒ log a miss, carry on. Do not resend. |
| `BOOT_TIMEOUT_MS` | 60000 | Arduino | Wake asserted but no traffic within 60 s ⇒ boot failed; re-assert wake once, then give up and flag. |
| `HALT_SETTLE_MS` | 20000 | Arduino | After `HALT`, wait this long before the wake line can do anything. See §3. |
| `SYNC_TIMEOUT_MS` | 1000 | Pi | No `SYNC` reply ⇒ retry up to 3 times, then log the failure. |

**No retransmission anywhere.** A lost event is data — it gets logged as a miss and shows up in the
detection rate, which is the thing being measured. Silently papering over losses would corrupt the
headline result.

---

## 2. Electrical contract — the two ways to destroy a Pi

Read this before connecting anything. Both failures are permanent and neither is recoverable.

### 2.1 GPIO3 must never see 5 V

The wake line is **Pi GPIO3 (BCM 3, physical pin 5)**. Pi GPIOs are **3.3 V and not 5 V tolerant.**

The Arduino drives it **open-drain only**:

```c
// Release the line (high-Z). The Pi's own 1.8 kΩ pull-up to 3V3 holds it high.
pinMode(PIN_WAKE, INPUT);

// Assert the line (pull low).
pinMode(PIN_WAKE, OUTPUT);
digitalWrite(PIN_WAKE, LOW);
```

> **`digitalWrite(PIN_WAKE, HIGH)` while the pin is an `OUTPUT` puts 5 V on GPIO3 and kills the Pi.**
> That line must not appear anywhere in the firmware. `INPUT` is how you release it. There is no
> `INPUT_PULLUP` here either — that connects the ATmega's pull-up to 5 V through ~30 kΩ, which will
> also push GPIO3 above 3.3 V.

GPIO3 is the I²C1 SCL pin and already carries a 1.8 kΩ pull-up to 3.3 V on the Pi board, so it is a
natural open-drain bus and needs no external pull-up. **Do not enable I²C on the Pi** — it would
fight the wake line.

Add a **1 kΩ series resistor** in the wake line as cheap insurance against a firmware slip: it
limits the fault current if the pin is ever driven high, giving the Pi's clamp diode a chance.

### 2.2 Arduino TX into the Pi's RX needs a divider

Arduino D1 (TX) idles at 5 V. Pi GPIO15 (RXD) is 3.3 V.

```
Arduino D1 ──[ 1.8 kΩ ]──┬── Pi GPIO15 (pin 10, RXD)
                         │
                       [ 3.3 kΩ ]
                         │
                        GND
```

5 V × 3.3/(1.8+3.3) = **3.24 V**. Any pair with roughly a 1:1.8 ratio works (1 kΩ / 1.8 kΩ,
10 kΩ / 18 kΩ). Prefer the lower-value pair: high-value dividers plus wire capacitance round off
the edges at higher baud. At 9600 this is not close to marginal.

The other direction — **Pi GPIO14 (TXD) → Arduino D0 (RX) — connects directly, no divider.** The
ATmega328P at 5 V needs V_IH ≥ 0.6 × VCC = 3.0 V and the Pi drives 3.3 V. It works, with ~0.3 V of
margin. If it ever proves flaky, the fix is a level shifter, not a pull-up to 5 V.

### 2.3 LM339N output is open-collector

The comparator **cannot source current.** Its output needs a **10 kΩ pull-up to 5 V** or it will
never go high and Tier 1 will never see a trigger. This is the single most common way this circuit
appears dead when it is actually working.

### 2.4 Power

The Arduino and the Pi are on **separate supplies**. Do not power the Arduino from a Pi USB port:
the Pi's USB rail is not guaranteed while it is halted, and Tier 1 must stay alive precisely then.
Do not power the Pi from the Arduino. Only **GND, wake line and the two serial wires** cross between
them — four wires total.

### 2.5 Wiring checklist, in this order

Do this before the Sep 10 integration. Skipping a step here costs a Pi.

1. Flash the firmware with the Pi **disconnected**.
2. Unplug the Arduino's USB cable.
3. Power the Arduino from its own supply. **Do not connect anything to the Pi yet.**
4. Meter Arduino D1 idle → expect ~5 V. Meter the divider's midpoint → **must read 3.2–3.3 V.**
5. Force the firmware into "wake asserted" and meter the wake-line pin → **must read < 0.4 V.**
   Force it into "released" and meter again → **must read open / floating, not 5 V.**
6. Only once 4 and 5 both pass: power off the Pi, connect GND first, then the wake line, then
   the two serial wires. Power the Pi last.

---

## 3. Wake and halt

### Pi states

| State | Power | Response to `EVT` | How it got there |
|---|---|---|---|
| **Awake** (daemon idle) | ~2.5 W | ~100 ms | booted and daemon running |
| **Halted** | ~0.5 W | **full boot, ~30 s — blind window** | `sudo halt` after a `HALT` |

There is no intermediate state. The Pi 4 has no usable suspend-to-RAM, which is exactly why the
dormancy question has a sharp answer and why hard-halt costs a full boot.

*These figures are the pre-measurement estimates. `E_boot`, `P_idle` and `P_halt` are measured on
Sep 7 and this table is updated with the real values.*

### Halt sequence

1. Arduino: dormancy timer expires → send `HALT`.
2. Pi: reply `ACK`, then `# halting`, then `sudo halt`.
3. Arduino: **start `HALT_SETTLE_MS` (20 s).** The wake line does nothing during shutdown, and
   asserting it mid-shutdown is how you end up with a Pi that halts and immediately refuses to wake.
4. After settle, the Arduino may treat the Pi as halted and wake it on the next event.

The Arduino must **release the wake line before sending `HALT`.** A held-low wake line during
shutdown prevents the wake from ever being seen as an edge.

### Wake sequence

1. Event arrives, Arduino believes the Pi is halted.
2. Assert wake (pull GPIO3 low) for **≥ 200 ms**, then release.
3. Start `BOOT_TIMEOUT_MS`. Do **not** send the `EVT` yet — nothing is listening.
4. The daemon sends `# ready` on startup. On seeing it, the Arduino sends the pending `EVT`.
   *If no `# ready` arrives before `BOOT_TIMEOUT_MS`, re-assert wake once, then flag and continue.*
5. The event that caused the wake is **recorded as a miss** unless the stimulus was still present
   when the daemon became ready. That miss is the measurement, not a bug.

**Only one pending event is buffered.** Events arriving during boot overwrite it. Tier 1 has no
queue and does not need one.

### Getting wake-from-halt working on the Pi

`GPIO3` wake from halt is a bootloader feature and is **on by default** on the Pi 4, but confirm it
rather than assuming:

```bash
sudo rpi-eeprom-config          # want: WAKE_ON_GPIO=1  and  POWER_OFF_ON_HALT=0
```

`POWER_OFF_ON_HALT=1` cuts more power (~0.01 W) but **disables GPIO3 wake entirely.** We need
`WAKE_ON_GPIO=1` and `POWER_OFF_ON_HALT=0`, which is why the halted floor is ~0.5 W rather than
near zero. Report that honestly — it is an architectural constraint, not a measurement error.

---

## 4. Clock reconciliation

Three clocks. **The Pi is the reference.** Everything is expressed in Pi time for analysis.

### Mac ↔ Pi

`ssh pi date` offset, measured **at the start and again at the end** of every run — drift over a
10-minute run is not negligible and a linear interpolation between the two is good enough.
`tools/run_experiment.py` does this automatically and records both in the run manifest.

### Mac stimulus ↔ Mac power log

Free. `event_display.py` and `fnb58_logger.py` run on the same machine and take timestamps from the
same `time.time()`. **This is the pair that matters most and it needs no reconciliation at all** —
which is exactly why power logging was moved to the Mac.

### Arduino ↔ Pi

`SYNC` round trip, initiated by the Pi:

```
Pi      → SYNC,<t_pi_ms>      at local t0
Arduino → SYNC,<t_ard_ms>     replies immediately, its own millis()
Pi      receives at local t1
offset = t_ard_ms − (t0 + t1)/2      # add to Arduino time to get Pi time
```

Take the **midpoint**, and take the **median of 5 round trips** — at 9600 baud one round trip is
~10 ms of transmission alone and a single sample is noisy. The Pi runs this at daemon startup and
logs the offset. Arduino timestamps are only ever used for jitter analysis, never for energy
segmentation, so a few ms of error is harmless.

### The clapperboard — the one that is trusted

At **t = 0 of every run**, the Pi burns 100% CPU on all 4 cores for **2 s** and logs the timestamp.
That produces an unmistakable ~2 s square step of several watts in the FNB58 trace, aligning the Mac
power log to Pi time to within one sample (10 ms) **without trusting NTP at all.**

This is the ground truth for alignment. The `ssh date` offset is the cross-check. If they disagree
by more than 100 ms, the clapperboard wins and the disagreement gets noted in the run log.

### Event alignment is by index, not timestamp

**The Nth `GEN` maps to the Nth `RES`.** Robust to every clock problem above. Timestamps are used
only for *energy segmentation* — deciding which slice of the power trace belongs to which event.

If `RES` count ≠ `GEN` count, the difference is the miss count, and the mapping is recovered by
walking both logs forward with the Pi's `state_at_evt` column marking where the misses happened.

---

## 5. Log formats

Four logs per run. All CSV with a header row, all written to `data/<run_id>/`.

### `gen.csv` — Mac, written by `event_display.py`

Ground truth. One row per stimulus event, written **at flash onset**, flushed immediately.

```
t_mac,event_idx,image_id,true_class,true_class_id,patch_contrast,duration_ms,is_target
```

- `t_mac` — epoch seconds, float, 3 decimals, taken as close to the frame flip as possible
- `event_idx` — 0-based, monotonic
- `image_id` — the stimulus filename, no path
- `true_class` / `true_class_id` — ImageNet label and index for that image
- `patch_contrast` — 0.0–1.0, the trigger patch's luminance step this event
- `duration_ms` — how long the flash was held
- `is_target` — `1` if `true_class` is the target class (banana), else `0`

Sub-threshold flicker events injected as Tier 0 false-positive bait are logged with
`image_id=NONE`, `true_class=NONE`, `true_class_id=-1`, `is_target=0` and their actual
`patch_contrast`. They are stimulus, and a Tier 0 firing on one is a false positive.

### `power.csv` — Mac, written by `fnb58_logger.py`

```
t_mac,sample_idx,voltage_V,current_A,power_W,dp_V,dn_V,temp_C,energy_J,charge_C
```

100 Hz. `energy_J` is a running integral from logger start; per-event energy is a difference of two
values, never a re-integration.

### `events.csv` — Pi, written by `pi_daemon.py`

```
t_pi,event_idx,arduino_t_ms,peak,evt_duration_ms,state_at_evt,capture_ms,infer_ms,latency_ms,class_id,class_name,confidence,top5,fired
```

- `state_at_evt` — `awake` or `booted` (this event caused a boot)
- `capture_ms` / `infer_ms` — the split of `latency_ms`
- `top5` — `id:conf;id:conf;...`, five entries, for post-hoc analysis without a re-run
- `fired` — `1` if the daemon declared the target class

### `manifest.json` — Mac, written by `run_experiment.py`

Every swept parameter, both clock offsets, git SHA, model file and its SHA256, start/stop times,
and free-text notes. **A run without a manifest is a run that did not happen.**

---

## 6. Firmware constants Juan owns

Exposed at the top of `tier1_firmware.ino` so a sweep can change one and reflash:

| Constant | Suggested start | What it does |
|---|---|---|
| `PERSIST_MS` | 40 | Trigger must stay asserted this long to count as an event |
| `DORMANCY_MS` | 30000 | **Silence before `HALT`. This is the swept variable.** |
| `REFRACTORY_MS` | 500 | Ignore new triggers for this long after an event |
| `WAKE_ASSERT_MS` | 200 | How long GPIO3 is held low |
| `PIN_TRIGGER` | 2 | INT0. Comparator output arrives here. |
| `PIN_WAKE` | 7 | To Pi GPIO3 through 1 kΩ. **Open-drain only.** |
| `PIN_PEAK_ADC` | A0 | Pre-comparator analog, for the `peak` field |

Sleep: `SLEEP_MODE_PWR_DOWN`, woken by **INT0 level-triggered (`LOW`)** on D2 — a power-down Uno's
edge detection is unavailable because the I/O clock is stopped, so level-triggered is the only
option that works. Wake, immediately `detachInterrupt`, then re-attach after the refractory period.

Disable the ADC (`ADCSRA &= ~(1 << ADEN)`) and use `power_all_disable()` before sleeping, or the
Uno sits at milliamps instead of microamps. **The onboard power LED and the USB-serial chip on an
Uno dominate sleep current regardless** — expect ~20 mA at the board level. Report the *ATmega's*
current with the DMM in the VCC line and say plainly that a board-level µW figure would need a bare
ATmega on a custom board. Honest beats impressive.

---

## 7. Mocks

Each side must be fully exercisable with the other absent. Both exist **before Sep 10.**

- **`tools/mock_arduino.py`** (Chris) — drives `pi_daemon.py` over a pty or a USB-TTL adapter.
  Emits `EVT` at a chosen rate, honours `ACK`/`RES`, answers `SYNC`, and can send `HALT`.
- **`tools/mock_pi.py`** (Juan) — connects to the Arduino's USB serial, answers `EVT` with `ACK`
  then a delayed `RES`, answers `SYNC`, and can simulate the halted state by going silent for 30 s
  so the boot path gets exercised without a Pi.

`mock_arduino.py --self-test` speaks both sides against itself, so the protocol parser is verified
before any hardware exists.
