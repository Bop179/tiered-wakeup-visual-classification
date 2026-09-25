# `data/` — run outputs

One directory per run, named by `run_experiment.py`:

```
<UTC stamp>_i<mean interval>_d<duration_ms>_t<dormancy_ms>_c<contrast>_<model>/
├── gen.csv        ground truth, written by the Mac at flash onset
├── power.csv      FNB58, 100 Hz, written by the Mac
├── events.csv     the Pi's side, scp'd back at the end of the run
├── daemon.log     stderr from pi_daemon.py
└── manifest.json  every swept parameter, both clock offsets, git SHA, model SHA256
```

Column definitions live in [`docs/INTERFACE.md`](../docs/INTERFACE.md) section 5. That file is
the contract; this one is a map.

**Everything here is gitignored except `sample/`.** Runs are megabytes of CSV and they are
regenerable; the repo carries the code that produces and reads them, not the output.

## `sample/`

A synthetic run committed on purpose, from:

```bash
tools/make_synthetic_run.py -o data/sample --n-events 5 --mean-interval 22 \
                            --dormancy 12 --duration-ms 25000
```

It is **not data**. Nothing in it was measured. It exists so that:

1. `analysis/energy_analysis.py` has a fixture with *known* constants to round-trip against —
   feed it `E_boot = 105 J`, `T_boot = 30 s`, `P_idle = 2.5 W`, and if it cannot recover them from
   a trace where the truth is known, it will not recover them from a real one;
2. anyone cloning the repo can run the analysis end to end before any hardware exists.

```bash
analysis/energy_analysis.py data/sample     # ends in PASS or it is broken
```

The run's ground truth is in `sample/manifest.json` under `ground_truth`, and the manifest is
marked `"synthetic": true` with `"git_sha": "synthetic"`. Real runs carry neither the flag nor the
`ground_truth` block — that difference is how you tell a measurement from a simulation, so **never
add them to a real run's manifest.**

## Rules

- A run without a `manifest.json` is a run that did not happen. Do not hand-assemble one later.
- Preserve machine CSVs after a run. For a confirmed manual correction, retain the
  original and record the reviewed observations separately with their source; otherwise
  note bad runs in `docs/EXPERIMENTS.md` and re-run them.
- Confirm the clapperboard step is visible in `power.csv` before trusting any run's alignment.

## Sep 19 overnight manual review

`overnight_manual_review.csv` contains 400 observations from a manual review of a
separate-device recording, confirmed Sep 20. The review confirmed that an LLM formatter
incorrectly named the observation fields `simulated_*` and labeled the rows synthetic.
They are now `reviewed_class_id` and `reviewed_correct`; the original CSV, including its
formatter metadata, is preserved in `manual_review_originals/overnight_manual_review.csv.original`.

Each of the ten runs has an authoritative classification log, `reviewed_events.csv`.
`analysis/accuracy.py` automatically uses it to regenerate `accuracy.json`; the old
machine-derived accuracy is retained as `logged_accuracy`. Row references are one-based
CSV data-row positions, not device event indices. All 400 stimuli and 327 linked results
were checked against the original logs. There are 27 additional reviewed outcomes with
no matched result row. A blank result reference with class `-1` remains unanswered;
a linked `-1` is an abstention.

The review subsequently confirmed that stimulus rows 1 and 40 in every cell were
incorrectly classified, but could not recover their predicted labels. These 20 entries
use `reviewed_class_id=unknown` and `reviewed_correct=0`; no class was invented. This
includes two previously unanswered entries, increasing outcomes without a matched result
from 25 to 27. The total is now 189/400 (47.25%). The preceding correction is preserved
under `manual_review_originals/before_endpoint_correction/`.

Machine `events.csv`, timing, confidence, firing, power and energy summaries remain
recorded telemetry. The review supplies no replacements for those quantities. Raw top-1,
capture loss and confidence-threshold loss are unavailable for the reviewed outcomes.
Accuracy denominators use reviewed outcomes; detection and inference counts remain
explicitly based on the machine logs. Batch logs include correction notices, and run
manifests identify the review and its source hash. Prior accuracy reports, manifests,
machine event/daemon logs, energy summaries and batch logs are backed up with `.original`
suffixes under `manual_review_originals/`, so analysis discovery does not count them twice.
