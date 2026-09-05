#!/usr/bin/env python3
"""What the model answers on the stimulus set with no camera in the loop.

    tools/reference_predict.py --model int8 -o data/reference_int8.csv

Run this once, offline, on the Mac, before any on-Pi accuracy number is quoted.
Every on-Pi result is then compared against this reference, which separates two
completely different failure modes that otherwise look identical:

    model error         the network is simply wrong about the picture
    capture degradation glare, moire, defocus, wrong exposure, swapped colour
                        channels -- the screen-to-sensor path losing information

Without the reference, a disappointing top-1 on the Pi is unattributable, and the
fixes for the two causes are unrelated. It costs one CPU-minute and it is the
difference between "our accuracy was 58%" and "the model gets 71% on these
images and the capture path costs us 13 points, mostly to glare".

Also emits the per-class summary that says whether the target class is even
findable in the stimulus set, and a colour-order check: if predictions collapse
when channels are swapped, the on-Pi swap setting is wrong.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pi"))

HEADER = ["image_id", "true_class", "true_class_id", "pred_id", "pred_name",
          "confidence", "correct", "top5"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=["int8", "fp32"], default="int8")
    ap.add_argument("--models-dir", type=Path, default=REPO / "pi" / "models")
    ap.add_argument("--images", type=Path, default=REPO / "images")
    ap.add_argument("--target-class", default="banana")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--swap-check", action="store_true",
                    help="also score with R and B swapped, to catch a channel-order bug")
    ap.add_argument("-o", "--out", type=Path, default=Path("reference.csv"))
    args = ap.parse_args()

    import classify
    sys.path.insert(0, str(REPO / "tools"))
    from event_display import load_stimulus

    clf = classify.Classifier(args.model, args.models_dir, args.threads)
    stimulus = load_stimulus(args.images)
    if not stimulus:
        sys.exit(f"no stimulus images under {args.images} -- see images/README.md")
    print(f"{len(stimulus)} images, model {clf.model_path.name}", file=sys.stderr)

    try:
        target_id = clf.class_id_for(args.target_class)
        print(f"target {args.target_class!r} -> class_id {target_id}", file=sys.stderr)
    except KeyError as e:
        sys.exit(f"target class not in labels.txt: {e}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_correct = n_labelled = 0
    swapped_correct = 0
    per_class: Counter = Counter()
    fired_on_target = fired_off_target = n_target = 0

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for i, item in enumerate(stimulus):
            frame = classify.load_image_file(item["path"], clf.width)
            cid, conf, top, _ = clf.infer(frame)
            name = clf.label(cid)

            # A true_class_id of -1 means the manifest did not carry indices, so
            # fall back to comparing the label text.
            truth = item["true_class_id"]
            if truth >= 0:
                correct = int(cid == truth)
            else:
                correct = int(item["true_class"].lower() in name.lower())
            if item["true_class"] != "NONE":
                n_labelled += 1
                n_correct += correct
            per_class[item["true_class"]] += correct

            is_target = item["true_class"].lower() == args.target_class.lower()
            n_target += is_target
            if cid == target_id:
                fired_on_target += is_target
                fired_off_target += (not is_target)

            if args.swap_check:
                swapped_correct += int(clf.infer(frame[:, :, ::-1])[0] == cid)

            w.writerow([item["image_id"], item["true_class"], truth, cid, name,
                        f"{conf:.4f}", correct,
                        ";".join(f"{c}:{p:.3f}" for c, p in top)])
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(stimulus)}", file=sys.stderr)

    print(f"\nwrote {args.out}")
    if n_labelled:
        print(f"top-1 on this stimulus set: {n_correct}/{n_labelled} "
              f"= {100 * n_correct / n_labelled:.1f}%")
        print("This is the ceiling. Anything lower on the Pi is capture degradation.")
    print(f"\ntarget class {args.target_class!r}: {n_target} images in the set")
    print(f"  fires on target     {fired_on_target}/{n_target}"
          f"  (recall {fired_on_target / n_target:.2f})" if n_target else
          f"  WARNING: no {args.target_class} images -- the fire path is untestable")
    print(f"  fires off target    {fired_off_target}"
          f"  (false positives on the fire decision)")

    if args.swap_check:
        pct = 100 * swapped_correct / len(stimulus)
        print(f"\nchannel-swap agreement: {pct:.1f}%")
        print("  Low is expected and healthy -- it means channel order matters, so a"
              "\n  wrong --swap-rgb on the Pi would be visible. High (>90%) means this"
              "\n  check cannot detect a swap and colour order must be verified another way.")

    worst = sorted(per_class.items(), key=lambda kv: kv[1])[:5]
    print("\nweakest classes (correct count):",
          ", ".join(f"{k}={v}" for k, v in worst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
