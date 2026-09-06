#!/usr/bin/env python3
"""Build images/ -- the ImageNet validation stimulus set -- and its manifest.

    .venv/bin/python tools/fetch_stimulus.py

Pulls real ILSVRC-2012 validation JPEGs from the `evanarlian/imagenet_1k_resized_256`
mirror on the HuggingFace datasets server (the official imagenet-1k repo is gated).
That mirror keeps the validation split sorted by label at exactly 50 images per
class, so class c occupies rows [c*50, c*50+50) and no per-row filtering is needed.
Images arrive resized to 256 on the short side, which is what we want: big enough to
fill a monitor region for the camera to photograph, small enough to fetch quickly.

Class selection is deliberate, not a random sample of ImageNet
---------------------------------------------------------------
    target  banana, the class the whole benchmark fires on.
    hard    yellow, elongated or otherwise produce-shaped classes -- lemon,
            pineapple, squash, corn, Granny Smith. These are the ones a banana
            detector actually trips on, so they are what give the false-positive
            rate any meaning. A stimulus set of bananas and golden retrievers
            would report a flattering number that says nothing.
    easy    dogs, cars, guitars, furniture -- no relationship to bananas at all.
            These keep the set honest: they are most of the real-world stream,
            and a detector that fires on them is broken in an obvious way.

THE INDEX CONVENTION, which is the thing to get right here
----------------------------------------------------------
The dataset labels classes in the 1000-class ImageNet space (banana = 954). The
model we ship emits 1001 classes with "background" at index 0, so every class sits
one higher (banana = 955), and that is the space manifest.csv must be written in --
see images/README.md. Rather than trusting `+ 1`, every class is resolved through
classify.Classifier.class_id_for() by name and cross-checked against it. A single
disagreement aborts the build before anything is downloaded, because an off-by-one
manifest scores every image wrong and reads as a capture failure rather than a
labelling bug.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pi"))

DATASET = "evanarlian/imagenet_1k_resized_256"
BASE = "https://datasets-server.huggingface.co"
PER_CLASS_TOTAL = 50          # the mirror's fixed validation depth

# (display name, group). The display name must be a name the model's labels.txt
# resolves, which is why these are the label's first comma-part, not a slug.
CLASSES = [
    ("banana",           "target"),

    ("lemon",            "hard"),
    ("orange",           "hard"),
    ("pineapple",        "hard"),
    ("Granny Smith",     "hard"),
    ("fig",              "hard"),
    ("spaghetti squash", "hard"),
    ("butternut squash", "hard"),
    ("zucchini",         "hard"),
    ("cucumber",         "hard"),
    ("bell pepper",      "hard"),
    ("ear",              "hard"),
    # Elongated tan/yellow things held in a hand -- the closest thing ImageNet has
    # to a banana that is not one. Deliberately not "corn": ImageNet splits corn
    # across "corn" and "ear, spike, capitulum", the model sends real corn images
    # to the latter, and carrying both would score a taxonomy quirk as vision error.
    ("hotdog",           "hard"),
    ("French loaf",      "hard"),

    ("golden retriever", "easy"),
    ("sports car",       "easy"),
    ("coffee mug",       "easy"),
    ("laptop",           "easy"),
    ("acoustic guitar",  "easy"),
    ("park bench",       "easy"),
    ("traffic light",    "easy"),
    ("umbrella",         "easy"),
    ("teapot",           "easy"),
    ("backpack",         "easy"),
    ("analog clock",     "easy"),
    ("barn",             "easy"),
]


def get_json(url: str, tries: int = 4):
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as fh:
                return json.load(fh)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == tries - 1:
                raise
            print(f"    retry {attempt + 1}/{tries - 1} ({type(e).__name__})",
                  file=sys.stderr)
            time.sleep(2 * (attempt + 1))


def fetch_bytes(url: str, tries: int = 4) -> bytes:
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as fh:
                return fh.read()
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def dataset_class_names() -> list[str]:
    """The mirror's 1000 ClassLabel names, in standard ImageNet order."""
    fr = get_json(f"{BASE}/first-rows?dataset={DATASET}&config=default&split=val")
    for feat in fr["features"]:
        if feat["name"] == "label":
            return feat["type"]["names"]
    raise SystemExit("no 'label' ClassLabel feature in the dataset")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", type=Path, default=REPO / "images")
    ap.add_argument("--n-target", type=int, default=50,
                    help="images of the target class (max 50)")
    ap.add_argument("--n-other", type=int, default=9,
                    help="images of every other class (max 50)")
    ap.add_argument("--models-dir", type=Path, default=REPO / "pi" / "models")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and cross-check the classes, download nothing")
    args = ap.parse_args()

    import classify

    clf = classify.Classifier("int8", args.models_dir, 1)
    print(f"model {clf.model_path.name}: {clf.n_classes} classes, "
          f"label offset {clf.label_offset}")

    names = dataset_class_names()
    by_name = {n: i for i, n in enumerate(names)}
    # A dataset label may be "laptop, laptop computer"; we name classes by the
    # first comma-part, so index that form too.
    for i, n in enumerate(names):
        by_name.setdefault(n.split(",")[0].strip(), i)

    # ---- resolve + cross-check every class before downloading anything --------
    plan, problems = [], []
    for display, group in CLASSES:
        ds_idx = by_name.get(display)
        if ds_idx is None:
            problems.append(f"{display!r}: not a class in the dataset")
            continue
        try:
            model_idx = clf.class_id_for(display)
        except KeyError:
            problems.append(f"{display!r}: not resolvable in the model's labels.txt")
            continue
        if model_idx != ds_idx + 1:
            problems.append(
                f"{display!r}: dataset says {ds_idx} (-> expected {ds_idx + 1}), "
                f"model resolves {model_idx} ({clf.label(model_idx)})")
            continue
        n = args.n_target if group == "target" else args.n_other
        plan.append({"display": display, "group": group, "ds_idx": ds_idx,
                     "model_idx": model_idx, "n": min(n, PER_CLASS_TOTAL)})

    if problems:
        print("\nrefusing to build -- class resolution disagrees:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("\nEvery class must resolve to dataset_index + 1. A mismatch means "
              "either a name collision in labels.txt or the wrong index space; "
              "either way the manifest would be wrong. See images/README.md.",
              file=sys.stderr)
        return 1

    total = sum(p["n"] for p in plan)
    n_target = sum(p["n"] for p in plan if p["group"] == "target")
    print(f"\n{len(plan)} classes cross-checked, all agree with the model.")
    for g in ("target", "hard", "easy"):
        sel = [p for p in plan if p["group"] == g]
        print(f"  {g:6s} {len(sel):2d} classes, {sum(p['n'] for p in sel):3d} images"
              f"   {', '.join(p['display'] for p in sel)}")
    print(f"  total  {total} images, target share {100 * n_target / total:.1f}%")

    if args.dry_run:
        print("\ndry run, nothing downloaded")
        return 0

    # ---- download ------------------------------------------------------------
    args.out.mkdir(parents=True, exist_ok=True)
    rows, failed = [], 0
    for p in plan:
        offset = p["ds_idx"] * PER_CLASS_TOTAL
        meta = get_json(f"{BASE}/rows?dataset={DATASET}&config=default&split=val"
                        f"&offset={offset}&length={p['n']}")
        got = 0
        for row in meta["rows"]:
            r = row["row"]
            if r["label"] != p["ds_idx"]:          # the offset maths is load-bearing
                print(f"  ! row {row['row_idx']} is label {r['label']}, "
                      f"expected {p['ds_idx']} -- skipping", file=sys.stderr)
                continue
            fname = f"{slug(p['display'])}_{row['row_idx']}.jpg"
            dest = args.out / fname
            if not dest.exists():
                try:
                    dest.write_bytes(fetch_bytes(r["image"]["src"]))
                except Exception as e:
                    print(f"  ! {fname}: {type(e).__name__} {e}", file=sys.stderr)
                    failed += 1
                    continue
            rows.append({"filename": fname, "true_class": p["display"],
                         "true_class_id": p["model_idx"]})
            got += 1
        print(f"  {p['display']:18s} ds {p['ds_idx']:4d} -> model {p['model_idx']:4d}"
              f"   {got:2d} images")

    manifest = args.out / "manifest.csv"
    with open(manifest, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["filename", "true_class", "true_class_id"])
        w.writeheader()
        w.writerows(rows)

    n_banana = sum(1 for r in rows if r["true_class"] == "banana")
    print(f"\nwrote {manifest} -- {len(rows)} images, "
          f"{n_banana} banana ({100 * n_banana / max(len(rows), 1):.1f}%)")
    if failed:
        print(f"{failed} downloads failed; re-run to fill the gaps "
              f"(existing files are skipped)", file=sys.stderr)
    print("\nNow establish the accuracy ceiling:\n"
          "  .venv/bin/python tools/reference_predict.py --images images "
          "-o data/reference.csv")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
