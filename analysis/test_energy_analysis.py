"""Run with: .venv/bin/python analysis/test_energy_analysis.py"""

import csv
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from energy_analysis import analyse_run


def main():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from plots import LIGHT, fig_trace

    args = SimpleNamespace(smooth_s=0, min_dwell_s=1, min_boot_s=8,
                           clapperboard=2, boot_cycle=False)
    with tempfile.TemporaryDirectory() as tmp:
        run = Path(tmp)
        # A long awake workload looks like a boot to the level-only detector.
        with (run / "power.csv").open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["t_mac", "power_W", "energy_J"])
            energy = 0
            for i in range(6001):
                t = i / 100
                watts = 3.8 if 20 <= t < 35 else 3.2
                energy += watts / 100
                writer.writerow([t, watts, energy])
        (run / "gen.csv").write_text(
            "t_mac,image_id,duration_ms\n20,test.jpg,10000\n")

        # Readback overrides the requested setting in both directions.
        for requested, verified in [(30000, -1), (-1, 30000), (-1, None)]:
            (run / "manifest.json").write_text(json.dumps({
                "params": {"dormancy_ms": requested},
                "dormancy_ms_verified": verified,
            }))
            out = analyse_run(run, args)
            if verified == -1:
                assert out["p_halt_est_W"] is None, out
                assert not out["halted_state_present"]
                assert out["p_boot_level_W"] is None
                assert out["boot_windows_t"] == []
                assert out["n_boot_windows"] == 0
                assert out["state_split_W"] is None
                assert out["frac_time_low_state"] is None
                assert abs(out["p_idle_est_W"] - 3.2) < 1e-6
                assert abs(out["energy_per_event_net_of_idle_J"] - 6) < 1e-6
                assert abs(out["energy_J_trapezoid"] - 201) < 1e-6
                # The plot's missing-summary fallback must use the same rule.
                fig = fig_trace(run, LIGHT, args, plt)
                assert not fig.axes[0].patches, "awake workload shaded as a boot"
                labels = [text.get_text() for text in fig.axes[0].texts]
                assert "P_idle 3.20 W" in labels, labels
                assert not any(label.startswith("P_halt") for label in labels)
                plt.close(fig)
            else:
                # A request alone must not conceal possible halts.
                assert out["halted_state_present"]
                assert out["n_boot_windows"] == 1
        print("PASS: verified never-halt state labels, energy, and readback precedence")


if __name__ == "__main__":
    main()
