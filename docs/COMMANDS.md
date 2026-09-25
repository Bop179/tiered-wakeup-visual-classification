# Commands we use often

Run everything from the repo root on the Mac. Anything that talks to the Pi uses the
ssh alias in `$PI_HOST` (default `pi`), so the Tailscale switch below covers
`run_experiment.py`, `live_dashboard.py` and `preview.sh` at once.

## Pi access

```bash
export PI_HOST=pi-ts        # use Tailscale: works from any network
unset PI_HOST               # back to the campus alias `pi`
findpi                      # campus alias stopped working: DHCP moved the Pi
```

**Pi back onto campus WiFi** (e.g. after the hotspot). Run it detached so it survives
the ssh drop, over Tailscale since the Pi changes networks mid-command:

```bash
ssh pi-ts 'sudo systemd-run --collect nmcli connection up PAL3.0'
# ~30 s later:
findpi && ssh pi 'nmcli -t -f NAME connection show --active'
```

## Camera

```bash
# Free the camera: stop the daemon (it sends SET,DORMANCY,-1 on the way out) and
# anything else holding it, then confirm nothing is left.
ssh $PI_HOST 'sudo systemctl stop tier3-daemon; pkill -f pi/preview.py; pkill -f pi/pi_daemon.py;
              sleep 1; pgrep -af "pi_daemon|preview|rpicam" || echo "camera free"'

tools/preview.sh            # live preview at http://localhost:8000, daemon's fixed exposure
tools/preview.sh --auto     # auto exposure, easier for aiming in a dark room
```

Ctrl-C stops the preview and frees the camera. It refuses to start while the daemon runs.

## Tier 1 flash + flicker test, read back from the Uno

The Uno on USB, **not** wired to the Pi (`ls /dev/cu.usb*` for the port). 15 s of black
counted for false triggers, then 5 real flashes (0.8) and 5 flickers (0.15), each 200 ms
like the demo's flicker:

```bash
.venv/bin/python tools/trigger_patch.py sweep --trimmer test \
    --contrasts 0.8 0.15 --per-contrast 5 --flash-ms 200 --quiet-s 15 \
    --serial /dev/cu.usbserial-10 --roc-out /tmp/roc.csv --detail-dir /tmp/roc_detail
```

Each flash prints detected / missed, and every Uno line comes through as `# uno: ...`.
`EVT` = Tier 2 accepted it. `# t2: noise N ms` = the comparator tripped but Tier 2
rejected it as too short. Expect 5/5 at 0.8 and 0/5 at 0.15. The `--roc-out` and
`--detail-dir` keep this test out of `data/tier1_roc.csv`.

Quick single flash, no counting: `.venv/bin/python tools/trigger_patch.py flash --count 1 --contrast 0.8 --serial /dev/cu.usbserial-10`

## Demos

### Before every demo

1. **Unplug the Uno's USB cable** and wire D0/D1 back to the Pi. With USB left in, the
   Uno cannot hear the Pi: no dormancy, no ACKs, and the Pi never sleeps. The
   2026-09-22 `demo_normal` run failed exactly this way.
2. **Power-cycle the Uno.** It keeps a stale "Pi halted" belief after bench work.
3. Pi awake and reachable: `ssh $PI_HOST uptime`. FNB58 on the Mac, stimulus monitor
   is display 1.
4. Start the dashboard (terminal 1) and leave it up. It switches to each new run by
   itself:

   ```bash
   .venv/bin/python tools/live_dashboard.py
   ```

   The title shows the mode **Tier 2 actually confirmed**. It turns red with "Tier 2 did
   NOT confirm" within seconds if step 1 was skipped. Stop and fix it; don't wait out
   the run.

Both demos include one dim flicker (0.15, 200 ms) between events. Tier 1 sensitivity is
the physical trimmer, so run each command once at the normal setting, then turn the
trimmer toward threshold and run it again with `sens` in `--tag`/`--trimmer`. At normal
the flicker does nothing. At high the comparator LED blinks on it, but Tier 2's 10 ms
persistence filter rejects it, so there's no event and no wake (Demo B, Sep 19: 94
blips, 0 false wakes).

### 1. Always on (never halts), about 5 min

20 events, 5 s each, 6-12 s apart. The awake path answers in ~50 ms, so there is no
waiting:

```bash
.venv/bin/python tools/run_experiment.py --dormancy-ms -1 \
    --n-events 20 --mean-interval 6 --dwell-dist fixed --duration-ms 5000 --first-dwell 2 \
    --flicker-rate 1 --flicker-contrast 0.15 --seed 651 --tag demo_awake_normal --trimmer normal
```

Dashboard: `ALWAYS ON`, the Pi stays AWAKE at ~3.3 W, a new result every ~13 s.

### 2. Normal operation: sleep, wake, classify, sleep again, about 5 min

Opens on an event while the Pi is awake, then 3 full halt/boot cycles, 25 s stimulus:

```bash
.venv/bin/python tools/run_experiment.py --dormancy-ms 15000 \
    --n-events 4 --mean-interval 20 --dwell-dist fixed --duration-ms 30000 --first-dwell 2 \
    --flicker-rate 1 --flicker-contrast 0.15 --seed 651 --tag demo_sleep_normal --trimmer normal
```

What the audience sees, per cycle:

| t after image onset | |
|---|---|
| 0 s | flash, Tier 1 fires. Event 1 is answered at once (Pi awake). Later ones wake the Pi: BOOTING, then ready ~21 s in while the image is still up, then classified |
| +15 s idle | Tier 2 sends `HALT`, dashboard drops to HALTED at 2.0 W |
| image end + 20 s | flicker. Pi stays asleep |
| image end + 50 s | next flash wakes it |

Why these numbers are the minimum: Tier 2 counts dormancy from the last link message
(the result), then ignores the wake line for 20 s (`HALT_SETTLE_MS`) while the Pi shuts
down. So a wake event must land at least boot (~21 s) + dormancy (15 s) + settle (20 s)
after the previous onset. The 50 s gap leaves ~19 s of margin for a slow boot. Seed 57
is the one that puts every flicker at 20 s, after the halt and away from the next
event. Another seed moves the flickers.

At the end, `run_experiment.py` wakes the Pi if needed and leaves it up for the next
run.

## Black screen

```bash
.venv/bin/python tools/event_display.py --n-events 0 --lead-in 1e9 -o /tmp/black.csv
```

Holds the stimulus monitor black until Ctrl-C (or Esc / `q` in the window).

Black screen with the dashboard (hand-held demo: flash the LDR with a phone
flashlight, hold an object in front of the camera):

```bash
.venv/bin/python tools/run_experiment.py --n-events 0 --mean-interval 20 --dormancy-ms 15000
.venv/bin/python tools/live_dashboard.py      # second terminal
```

`--n-events 0` makes it a `_manual` run: meter and daemon run as usual, the screen
stays black until Esc / `q` in the window, then the Pi is released as normal. No row
goes into EXPERIMENTS.md. If the flash woke the Pi, keep the object in view for ~21 s
until it boots, because the camera only captures once the daemon is ready.
Use `--dormancy-ms -1` if you want it to stay awake.
