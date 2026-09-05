# `firmware/tier2_firmware/` — Tier 2, Arduino Uno (Juan)

`tier2_firmware.ino` goes here. It is **not** in the repo yet because it is Juan's deliverable,
not something to be written for him — the interface contract is what he needs from our side, and
that is already frozen.

## What to read first, in this order

1. **[`docs/INTERFACE.md`](../../docs/INTERFACE.md)** — the frozen contract. Section 1 is the
   serial link, section 3 wake and halt, section 6 the firmware constants you own.
   **Section 2 is "the two ways to destroy a Pi". Read it before wiring anything.**
2. **[`docs/TEAMMATE_BRIEF.md`](../../docs/TEAMMATE_BRIEF.md)** — the Tier 1 analog chain and the
   Tier 2 spec in full.

## The job in one paragraph

Sleep in `SLEEP_MODE_PWR_DOWN`. Wake on the Tier 1 comparator via **INT0, level-triggered (`LOW`)**
on D2 — a powered-down Uno has its I/O clock stopped, so edge detection does not work and
level-triggered is the only option. Confirm the trigger persisted for `PERSIST_MS` before believing
it. Send `EVT` to the Pi, waking it first over the open-drain GPIO3 line if it is halted. After
`DORMANCY_MS` of silence, send `HALT`. Go back to sleep.

`DORMANCY_MS` is **the swept variable of the entire experiment** — expose it at the top of the file
so a sweep is a reflash, not an edit.

## Two things that will bite

- **GPIO3 must never see 5 V.** Drive it open-drain only: `pinMode(INPUT)` to release,
  `pinMode(OUTPUT); digitalWrite(LOW)` to assert. Never `OUTPUT HIGH`. Meter it before connecting.
- **`power_all_disable()` and `ADCSRA &= ~(1 << ADEN)` before sleeping**, or the Uno sits at
  milliamps instead of microamps. Even then the board-level current stays around 20 mA because the
  power LED and USB-serial chip cannot be turned off in software. Report the ATmega's own current
  separately, measured with the DMM in the VCC line, and say plainly which is which.

## Developing with no Pi attached

`tools/mock_pi.py` (also Juan's) answers `EVT` with `ACK` then a delayed `RES`, answers `SYNC`, and
can go silent for 30 s to simulate the halted-and-booting Pi. Build against it so that the Sep 10
integration session is a two-hour check, not a debugging session.

Our side is already exercisable the same way — `python3 tools/mock_arduino.py --self-test` speaks
both halves of the protocol against itself and passes today, including every malformed-input case.
