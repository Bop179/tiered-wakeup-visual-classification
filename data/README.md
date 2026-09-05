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
                            --dormancy 12 --duration-ms 15000
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
- Do not edit a CSV after a run. If a run is bad, note why in `docs/EXPERIMENTS.md` and re-run it.
- Confirm the clapperboard step is visible in `power.csv` before trusting any run's alignment.
