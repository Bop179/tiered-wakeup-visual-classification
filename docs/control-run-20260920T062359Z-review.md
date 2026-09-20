# Review of the 30-second positive control

Run: `20260920T062359Z_i60_d30000_t15000_c0.8_int8_control`

**This control did not succeed. It does not establish whether extending the stimulus beyond the 20.8-second boot time fixes classification.** The run recorded no new boot, missed one stimulus, and returned `none` for the other. Communication failures prevented verification of the requested dormancy setting.

| Measurement | Result |
|---|---:|
| Displayed stimuli | 2, each configured for 30 seconds |
| Stimuli matched to a classification attempt | 1/2 (50%) |
| Correct classifications across all stimuli | 0/2 (0%) |
| Banana target fires | 0/1 |
| Logged daemon starts | 1, with Pi uptime already 183.6 seconds |
| Results marked as following a boot | 0 |
| Requested dormancy | 15 seconds; unverified |
| Power recording | 205.097 seconds, 20,524 samples |
| Total energy | 665.926 J |
| Mean power | 3.247 W |

## What happened to the images

Times below are local EDT; the CSV timestamps are Unix seconds.

| Stimulus onset | Image | Outcome |
|---|---|---|
| 02:25:22.933 | `banana_47701.jpg` | No matching classification result; no target fire |
| 02:26:52.952 | `lemon_47551.jpg` | One awake-state attempt, returning `class_id=-1`, `none` |

The lone result aligns with the lemon onset, not the banana. Its `event_idx=0` counts daemon results, so matching it to stimulus 0 by index would be wrong. Using the initial clock offset puts receipt approximately 5 ms before the logged lemon onset. This small discrepancy is below the precision supported by the clock synchronization; it is not proof of a physically premature capture.

The attempt took 52 ms overall: 3.4 ms capture and 38.2 ms inference. Its raw top prediction was class 621 at 14.3% confidence; lemon (952) was absent from the top five. The daemon then applied its confidence threshold and wrote `none,0.000`. Lowering the threshold would still leave the top prediction wrong.

Keeping an image visible for 30 seconds does not guarantee a later retry: the daemon captures once per received event. An unsuitable frame at onset could therefore still fail. A stale frame, framing/exposure problem, or model error remains possible, but this run saved no camera frame that establishes which occurred.

## The main problem is the control link

The daemon log contains all of these messages:

```text
# sync failed (no Arduino?)
# WARN DORMANCY not acknowledged -- firmware may predate SET/GET; the value in effect is whatever was flashed
# t2rx # t2: boot timeout -- re-asserting wake once
# t2rx # t2: boot failed -- giving up on this event
# evt 0 awake 52ms -> -1 none 0.000
# t2rx # t2: no ACK -- assuming halted
# t2rx # t2: wake asserted
# WARN exit-dormancy not acknowledged
```

The Pi received Tier 2 diagnostics and an event. The checked-in daemon sends an ACK before capturing that event, yet Tier 2 subsequently reported no ACK. Synchronization, setting dormancy, and disabling dormancy at exit also failed to receive acknowledgments.

**A failure in the Pi-to-Arduino communication direction is the leading hypothesis.** Wiring, UART configuration, or firmware receive/parsing behavior could explain it. The generic “firmware may predate SET/GET” warning is not a diagnosis. These files do not identify the deployed firmware version or prove a specific hardware fault.

Likewise, Tier 2's “boot failed” message reflects its state machine timing out; it does not establish that the Pi physically attempted and failed a boot. The only recorded daemon start had substantial existing uptime, and there is no logged halt or subsequent daemon start.

## Power interpretation

Independent trapezoidal integration gives 665.926 J, agreeing with the meter's 665.928 J counter. Sample indices are continuous, the largest timestamp gap is 14 ms, and there are 11 repeated millisecond timestamps with no backward timestamps.

The 3.247 W average is close to the project's previously measured 3.26 W idle baseline. The approximately 0.4% difference does not establish savings; this is not a matched baseline comparison. No sample fell below 2.3 W, whereas the stored halted baseline is 1.997 W. Together with the boot log, this strongly suggests the Pi remained running throughout this recording.

The energy script automatically labels two clusters `P_halt=3.188 W` and `P_idle=3.448 W`. **Do not treat those labels as demonstrated hardware states here.** There is no confirmed halt cycle. The reported negative incremental event energy (-7.86 J relative to inferred idle) is consequently not a useful event-cost measurement. The script found no boot windows.

The energy script's clapperboard alignment and SSH clock alignment also disagree by 0.209 seconds. That does not change which stimulus the single result belongs to, but it rules out a precise onset-to-capture latency claim from this run.

## Next checks

1. Restore and verify two-way serial communication: successful `SYNC`, a confirmed `DORMANCY=15000` response, and an event ACK/RES exchange accepted by Tier 2. Inspect the Pi TX to Arduino RX path and serial configuration first, while keeping firmware compatibility as another possibility.
2. While the Pi is awake, capture and inspect an actual frame with a known image already stable on screen. This separates classification/camera problems from wake sequencing.
3. Repeat the long-stimulus control only after confirming a real halted power plateau, wake, new boot ID, and classification while the stimulus remains visible. Confirm exit dormancy is acknowledged too.

Retain this run as evidence of a failed control with unverified dormancy. It neither disproves nor validates the earlier short-stimulus/boot-time explanation. Two stimuli would also be insufficient for a reliable accuracy estimate even after the control path works.

## Sources and verification

Reviewed the [run directory](../data/20260920T062359Z_i60_d30000_t15000_c0.8_int8_control/): `manifest.json`, `gen.csv`, `events.csv`, `boots.csv`, `daemon.log`, and `power.csv`; compared with [stored constants](../data/constants.json), [daemon behavior](../pi/pi_daemon.py), [camera capture](../pi/classify.py), and [checked-in firmware](../firmware/tier2_firmware/tier2_firmware.ino). Checked-in code is supporting context, not proof of deployed versions.

Both existing analysis scripts completed successfully with `--no-write`; event matching and power integration were independently checked. No raw data or existing analysis outputs were changed.

```sh
.venv/bin/python analysis/accuracy.py data/20260920T062359Z_i60_d30000_t15000_c0.8_int8_control --no-write
.venv/bin/python analysis/energy_analysis.py data/20260920T062359Z_i60_d30000_t15000_c0.8_int8_control --no-write
```
