#!/bin/zsh
# Overnight matrix, priority-ordered: stopping at any point leaves complete cells.
# One run_experiment.py per cell; a failed cell is logged and skipped, not retried.
#   tools/overnight.sh            # run
#   DRY=1 tools/overnight.sh      # schedules and manifests only, no hardware
#   tools/overnight.sh 5          # skip the first 5 cells (resume after a failure)
# Stop between cells: touch overnight_stop (the running cell finishes first).
cd "${0:A:h}/.." || exit 1
DUR=30000   # 30 s stimulus outlasts T_boot (20.8 s, s18_tboot) by ~9 s
# interval_s dormancy_ms  (-1 = never halt)
CELLS=(
  "20 30000"  "20 -1"  "45 30000"  "45 -1"      # core: halt vs never, 2 rates (~3.1 h)
  "120 30000" "120 -1"                           # sparse end, biggest saving (~3.8 h)
  "20 15000"  "45 15000"  "20 60000"  "45 60000"  # dormancy shape (~3.1 h)
)
CELLS=(${CELLS[@]:${1:-0}})   # tools/overnight.sh 5  -> resume after 5 done cells
log=data/overnight_$(date +%m%d_%H%M).log
rm -f overnight_stop
for c in $CELLS; do
  [[ -e overnight_stop ]] && { echo "stop file, done" | tee -a $log; break; }
  set -- ${=c}
  echo "=== $(date +%T) interval $1 s, dormancy $2 ms" | tee -a $log
  caffeinate -dims .venv/bin/python tools/run_experiment.py --mean-interval $1 \
      --dormancy-ms $2 --duration-ms $DUR --n-events 40 --tag overnight \
      ${DRY:+--dry-run} 2>&1 | tee -a $log
  echo "=== exit ${pipestatus[1]}" | tee -a $log
done
