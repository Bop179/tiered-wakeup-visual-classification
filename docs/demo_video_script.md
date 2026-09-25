# Demo video — plan and script

**10:00 target. The video is the entire submission** (no repo, report, or deck is graded), so
every claim the project wants credit for has to be said out loud or shown on screen here.

Two voices: **C** = the Tier 3 owner (Tier 3, measurement, analysis), **J** = the Tier 2 owner (Tier 1 analog, Tier 2
firmware). Alternating keeps ten minutes awake. If only one person records, keep the same
segments and drop the initials.

Narration is written to be read at ~140 wpm. Don't ad-lib past a segment's time — the last two
segments are the ones that win the project and they're at the end.

---

## 0. Budget

| # | Segment | In | Length |
|---|---|---|---|
| 1 | Cold open — the rig doing the thing | 0:00 | 0:30 |
| 2 | The problem | 0:30 | 1:00 |
| 3 | The architecture we propose | 1:30 | 1:30 |
| 4 | How it's actually built | 3:00 | 1:30 |
| 5 | **Demo A** — the cascade working, end to end | 4:30 | 1:45 |
| 6 | Data and analysis | 6:15 | 2:00 |
| 7 | **Demo B** — trigger too sensitive / too insensitive | 8:15 | 1:15 |
| 8 | What it cost, what we'd fix | 9:30 | 0:30 |

---

## 1. Cold open (0:00–0:30)

**SHOW:** No titles first. Straight to a locked-off shot of the rig: monitor showing black, the
breadboard with the LDR aimed at the corner patch, the Uno, the Pi with the HQ camera. A banana
image flashes on the monitor. Cut in tight on the comparator LED lighting. Cut to the terminal:
`EVT,...` then `RES,banana,0.83,21`. Title card over the last two seconds.

**SAY (C):** "This is a camera that spends almost all of its life switched off. A light-change
detector made of an LDR and two op-amps — about twelve milliwatts — decides when a Raspberry Pi
is allowed to wake up, take one picture, classify it, and go back to sleep. We built it, we
measured it, and the measurement says something we did not expect."

*Title card: **Tiered Wake-Up: how much energy does an always-on camera actually need?** ·
Purdue Chips & AI Hackathon*

---

## 2. The problem (0:30–1:30)

**SHOW:** One slide. Left: "always-on camera, MobileNetV2 on every frame". Right: a bar of
wall-clock time, 99% of it shaded "nothing happened". Then a second slide with the two-sided
tradeoff: an axis labelled *dormancy timeout*, energy falling, detection falling with it.

**SAY (C):** "A camera that runs a neural network on every frame spends nearly all of its energy
on scenes where nothing happened. The obvious fix is to keep the expensive stage asleep and wake
it when something changes.

But sleeping is not free. Bringing a halted Raspberry Pi 4 back is a full boot — we measured
twenty-point-eight seconds — and the camera is blind for every second of it. Halting only saves
about one and a quarter watts against sitting idle. So dormancy pays for itself only if the
machine stays down long enough, and if it stays down too long, it misses the thing it was built
to see.

That is a real two-sided tradeoff between average power and detection rate, and the break-even
moves with how often events happen. Predicting where that boundary sits, and then measuring
whether the prediction holds, is the actual deliverable of this project. The cascade is the
instrument. The number is the result."

---

## 3. The architecture (1:30–3:00)

**SHOW:** The block diagram from the README, animated in three beats — Tier 1, then Tier 2, then
Tier 3 — with the power figure appearing next to each tier as it lands. Keep the two links
labelled: **GPIO3 open-drain wake** and **UART 9600, EVT / ACK / RES**.

**SAY (J):** "Three tiers. Each one only decides whether the next one needs to wake up.

Tier one is entirely analog — no clock, no code. A photosensor, a high-pass that throws away the
ambient light level so only *change* survives, a gain stage, a low-pass at about forty-eight hertz
to kill mains hum and display flicker, and a comparator with a trimmer that sets the threshold.
Twelve milliwatts, always on. That's our measurement, with a meter in series.

Tier two is an Arduino Uno asleep in power-down, woken on the comparator's edge through INT0. Its
job is judgement: confirm the trigger actually persisted, reject one-off noise, and decide when to
wake tier three — and, just as importantly, when to put it back to sleep. That dormancy timeout is
the one parameter the whole experiment turns on. The whole board costs about seventy-seven
milliwatts asleep and a hundred and fifty-four awake — whole-board, including the power LED and
the USB chip we can't switch off in software.

Tier three is a Raspberry Pi 4 with the HQ camera, running stock MobileNetV2 INT8 on ImageNet.
Three-point-two-six watts awake. Two watts halted."

**SAY (C):** "Two watts halted, not zero, and that constraint is the design. Waking the Pi from a
GPIO pin requires `POWER_OFF_ON_HALT=0`, which keeps the always-on rail energised while it is
halted. The Pi 4 has no usable suspend-to-RAM, so there is no middle state: it is awake, or it is
off and twenty-one seconds away from being useful."

---

## 4. How it's built (3:00–4:30)

**SHOW:** Close, well-lit pans over the real hardware while each is named. Then a 15-second
screen recording of the benchmark: the monitor with the trigger patch in one corner and the
ImageNet image in the middle. Then one glance at a terminal showing a live `EVT → ACK → RES`
exchange.

**SAY (J):** "Here it is as built. The sensor is an LDR in a divider with a trimmer for the load —
that trimmer is an ambient-light calibration, not a design constant, and we'll come back to why we
know that. The chain is an LM358 and an LM339 comparator with hysteresis. The Uno takes the
comparator output on D2, samples the analog level on A0 to record how big the flash was, and talks
to the Pi over two wires: a serial link, and an open-drain line onto GPIO3 that pulls the Pi out
of halt."

**SAY (C):** "To measure any of this we need ground truth, so events come from a script on a Mac
driving the monitor the rig watches. Two independent regions are on screen at once, and the
separation is the point. A plain luminance patch in the corner is what tier one's sensor is aimed
at, and its contrast sweeps on its own — so tier one's sensitivity isn't confounded by whether a
given photograph happens to be a dark night scene. The middle of the screen shows an ImageNet
image, which is what the Pi's camera frames and classifies. We log what was shown and when, the
Arduino logs what it detected, the Pi logs what it answered, and an inline USB power meter logs
the Pi's rail at a hundred hertz. Every number in the rest of this video comes out of those four
logs."

---

## 5. Demo A — the cascade working (4:30–6:15)

Shoot this as one continuous take, four-up on screen: **monitor** (top left), **breadboard with
the comparator LED** (top right), **serial log** (bottom left), **live power trace** (bottom
right). Do not cut away during the boot — the dead time is the result.

**Beat 1 — awake path (~25 s).** Pi already awake, dormancy `-1`.

**SAY (C):** "First, the easy case: the Pi is already awake. Watch all four panes."

*Flash a banana image.* LED lights → `EVT` → `ACK` → `RES,banana`. Freeze one frame, overlay
**`event → answer: 42 ms`**.

**SAY (C):** "Trigger, wake interrupt, capture, classify, answer. Forty-two milliseconds, and the
inference itself is twenty-one of them and sixty-two millijoules. That is the system working as
advertised."

**Beat 2 — the halt (~20 s).** Send `SET,DORMANCY,15000` on camera, or just let the timeout run.

**SAY (J):** "Now we let it go quiet. Tier two counts out the dormancy timeout, tells the Pi to
halt, and the Pi goes down."

*Power pane: the step from 3.26 W to 2.0 W. Overlay the two numbers on the trace.*

**Beat 3 — the wake, in real time (~35 s).** Start a visible timer overlay on the cut.

**SAY (J):** "Everything from here is the part a slide deck would hide. New event. Tier one fires,
tier two pulls GPIO3 low, and the Pi starts booting — and we are going to sit here for all of it."

*Run it real time. Timer counts. Hold the stimulus for 25 s. The Pi should become ready
at about 20.8 s while the image is still visible; keep the result and timer in frame.*

**SAY (C):** "Twenty-point-eight seconds to become ready. Our stimulus lasts twenty-five
seconds, leaving about four seconds to capture the image after boot. Watch the actual
classification result: the timing now allows the Pi to see the image that woke it."

---

## 6. Data and analysis (6:15–8:15)

**Historical results:** Label the existing matrix figures as the earlier short-stimulus
runs. They do not measure the corrected 25 s configuration. Replace them only after a
new matrix run, and update the numerical narration to match that run.

**SHOW:** In order — (a) the constants table, (b) `analysis/figures/trace.png`, (c) the
pre-registered-vs-measured Pareto, (d) `analysis/figures/pareto.png` with the fitted lines, (e) the
accuracy table. One figure per claim; don't crowd them.

**SAY (C):** "These are the historical short-stimulus results: ten cells, overnight, forty
events each. Three event rates crossed with four
dormancy settings.

Start with the constants, because two of them moved the project. A halted Pi costs two watts, not
the half-watt we planned for — so halting only buys one-point-two-six watts. And the power a boot
draws is within ten milliwatts of the power of just sitting idle. Booting is not an energy cost.
It is a *blind window*.

We committed the model to the repository before the matrix ran, and it makes three falsifiable
predictions. First: because a boot costs almost no extra power, the break-even event interval is
effectively zero — at any event rate a human could produce, halting saves energy. Second: the
power-optimal timeout is zero or infinity, never in between — an intermediate timeout is not an
energy optimisation, it is detection rate you are buying. Third, the sharp one: the Pareto front
of power against detection should be exactly a straight line, with a slope we wrote down in
advance.

Measured: at a forty-five-second mean interval the front is straight to an r-squared of
nought-point-nine-eight-nine, and the slope is within eight percent of the number we registered
before the run. At a hundred and twenty seconds, within six percent. At twenty seconds it's
twenty-six percent too steep — and that miss is ours, not the model's: our stimulus script draws
each random gap *after* the previous image ends, so the real mean gap is inflated, worst at the
shortest interval. The error runs in exactly the direction the fit quality ranks. And every
halting cell drew less power than its never-halting partner at every interval, down to twenty
seconds — no crossover, as predicted."

**SAY (C, over the accuracy table):** "Across four hundred stimuli, one hundred eighty-nine were
classified correctly: forty-seven-point-two-five percent. The never-halting cells scored
forty-seven-and-a-half to fifty-two-and-a-half percent; the halting cells scored thirty-seven-and-a-half
to fifty-seven-and-a-half percent, against a seventy-percent reference ceiling. Accuracy does not
move consistently with dormancy in these results.

Pi-rail energy savings still reach twenty-seven percent. The boot takes longer than the historical
fifteen-second stimulus, which could affect capture, but these results do not isolate that effect.
Our current twenty-five-second stimulus outlasts the measured boot; we need a new controlled matrix
to quantify the effect on accuracy and energy."

---

## 7. Demo B — the trigger, too sensitive and not sensitive enough (8:15–9:30)

This is the sensitivity demo, and it doubles as the ROC we never had time to sweep — three honest
points instead of a curve. Hold the stimulus **constant** across all three takes: real flashes
*plus* deliberate sub-threshold flicker as false-positive bait.

```sh
.venv/bin/python tools/event_display.py --n-events 6 --mean-interval 12 --duration-ms 4000 \
    --flicker-rate 6 --flicker-contrast 0.15
```

Two-up: **monitor** and **serial log**. Put a caption in the corner naming the setting. Sweep one
knob at a time and say which.

All three shot Sep 19 with the trimmer as the only knob, `PERSIST` held at 10 ms, and capture
disabled (`--no-camera`) — this segment measures Tier 1's threshold, so the classifier is a
confound it does not need. **The class column in these logs is a fixed stub; do not narrate it.**
Data: `data/demoB/{normal,sens,lowsens}.{csv,log}`.

| Take | Trimmer | Measured | Caption |
|---|---|---|---|
| 1 — as locked | as run overnight | **4/4** real flashes → `EVT`; 4 baits → 0 accepted, 2 logged `# t2: noise 4 ms` | *as run: 4 of 4, bait rejected* |
| 2 — sensitive | toward threshold | **94** comparator blips at 0–1 ms, **all rejected**; still 4/4 real; **0 false wakes** | *94 blips, 0 wakes* |
| 3 — insensitive | away from threshold | **0 triggers of any kind** — the LED never lights | *nothing gets through* |

**SAY (J):** "Tier one has two knobs: a hardware threshold on the comparator trimmer, and a
software persistence time in the firmware — how long the comparator has to hold before tier two
believes it. Same stimulus in all three of these: four real flashes, plus deliberate low-contrast
flicker as bait.

This is the setting we ran the matrix at. Four real flashes, four caught. The bait gets rejected —
you can see tier two logging it as noise, four milliseconds, too short to believe.

Now I turn the threshold down until tier one is firing on nothing. *(turn the trimmer)* Ninety-four
times in two minutes the comparator trips, and look at what tier three does about it: **nothing.**
Every one of those blips lasted under a millisecond, and tier two's persistence filter threw away
all ninety-four while still catching all four real flashes. That is the thing we did not expect and
it is the best argument for the architecture: tier one can be badly miscalibrated and the system
still does not wake up. You would have to break the firmware too.

And now the other way. *(turn the trimmer back past centre)* Nothing gets through at all — the
indicator LED never lights, and the log stays empty for the whole two minutes. A tier one that
cannot see is indistinguishable from a tier one that isn't there.

We have been on the wrong side of that filter too, in the other direction: persistence shipped at
forty milliseconds and was silently rejecting real flashes. Ten fixed it. The filter that saved us
here is the same one that cost us a week when it was set wrong.

We should be straight about this: the threshold was bracketed by hand — find where the comparator
just turns on, find where it just turns off, sit in the middle — not chosen off a measured
sensitivity curve. These three points are the shape of that curve, not the curve."

---

## 8. Limits and close (9:30–10:00)

**SHOW:** One slide, four bullets, then the last frame holds on the headline sentence.

**SAY (C):** "What we'd want a reviewer to hold us to. The savings we quote are the Pi rail
against an always-on Pi rail — but we did go and meter the cascade's own cost, and it comes to
about a hundred and ten milliwatts against Pi-rail savings of point-two-one to point-seven-three
watts. So the cascade is net-positive at every cell we ran, and it's cheapest relative to the
saving at *long* intervals, which is the opposite of what we guessed. The halted floor is two
watts because GPIO wake requires it; a genuinely power-gated tier three would take our best cell
from twenty-seven percent saved to a projected seventy-three, and that number is arithmetic, not a
measurement. And it is one night, one run per cell, forty events — about eight points of binomial
noise on every detection rate.

What we built is a three-tier wake-up cascade that works end to end. The corrected stimulus
lasts twenty-five seconds against a twenty-point-eight-second boot, leaving about four seconds
for capture. The earlier short-stimulus results show why wake latency matters; a new matrix
will tell us the tradeoff with the corrected duration."

---

## Pre-flight checklist

Do all of this before recording anything.

- [ ] Re-tune the LDR load trimmer **in the room you record in** — ambient light moves the
      operating point. Confirm the ADC peak in `EVT` field 3 is near 645, idle near 512.
- [ ] Patch fraction back to **0.25**. At 0.18 the sensor only partly overlaps and the comparator
      chatters; it looks exactly like a dead sensor.
- [ ] Wake line resistor is **330 Ω**, not 1 kΩ, or GPIO3 never pulls low.
- [ ] `vcgencmd get_throttled` → `0x0` on the Pi.
- [ ] Take the schematic/breadboard photo (still outstanding in `trigger_characterization.md`) —
      it's the one still image segment 4 needs.
- [ ] Run one `tools/run_experiment.py --mean-interval 20 --duration-ms 25000 --dormancy-ms 15000 --n-events 4`
      rehearsal to confirm the whole chain works before the camera rolls.
- [ ] Regenerate figures so what's on screen matches what's in the repo:
      `analysis/plots.py data/ --tag overnight` and the named-cell trace.
- [ ] Record room audio separately if you can; breadboard close-ups have no useful sound.

## Shoot order (not the edit order)

Hardware first while the rig is set up and calibrated, slides last — slides can be re-recorded at
3 a.m., a calibrated rig cannot.

1. Demo B, all three takes (same stimulus run, three settings — do it in one sitting).
2. Demo A, one continuous four-up take. Get two or three; the boot is the one part you can't fake.
3. Cold open (needs a clean flash and a clean `RES,banana`).
4. Hardware b-roll and the breadboard pans.
5. Voice-over for segments 2, 3, 6, 8 over slides and figures.

## Fallbacks

- **Pi won't wake on camera.** Cut to the power trace from a matrix run and narrate it — the
  staircase is the same evidence. Don't burn take after take live.
- **Running long.** Cut segment 4 to 60 s by dropping the benchmark-design explanation; keep
  segments 5, 6, 7 intact. They are the project.
- **Solo recording.** Merge J's lines into C's; nothing in the script depends on two speakers.
- **Nothing on the rig works at all.** Segments 5 and 7 can be rebuilt from `data/` — every run
  has `events.csv`, `power.csv` and a daemon log with the serial exchange in it.
