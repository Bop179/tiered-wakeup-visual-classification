#!/usr/bin/env python3
"""Monitor-driven stimulus generator -- the event source for the whole benchmark.

    tools/event_display.py --n-events 40 --mean-interval 45 --duration-ms 15000 \
                           --contrast 0.8 --out data/<run_id>/gen.csv

Displays, full screen on a monitor the rig watches:

    black dwell (length sets the event rate)
      -> flash: trigger patch + ImageNet image, held for the event duration
           -> back to black

Two independent regions, and the separation is the point:

  TRIGGER PATCH  a plain luminance square in a corner, which Tier 1's photosensor
                 is aimed at. Its contrast sweeps on its own, so Tier 1's ROC is
                 not confounded by whether a given photo is a dark night scene.
  IMAGE REGION   what the HQ camera frames and Tier 3 classifies.

Arrival distribution
--------------------
Exponential by default, and that matters. With a FIXED dwell the Pi is either
always awake at an arrival or always halted at one, so detection rate collapses
to a step and there is no Pareto front to plot. Memoryless arrivals give a smooth
front, and they are what analysis/power_model.py solves, so prediction and
measurement are comparable. Use --dwell-dist fixed only as a timing sanity check.

Ground truth
------------
Writes gen.csv per docs/INTERFACE.md section 5, one row at each flash ONSET,
flushed immediately. The timestamp is taken right after the frame flip returns,
so it is as close to photons as this side can get.

Sub-threshold flicker events are injected as Tier 1 false-positive bait. They are
stimulus, logged with image_id=NONE and their real contrast; a Tier 1 firing on
one is a false positive, not a miss.

--dry-run walks the identical schedule with no window and no pygame, so the log
can be verified with no hardware and no dependencies at all. The schedule is
generated from --seed before anything is drawn, so a dry run and the real run
with the same seed produce the same events in the same order.

Stimulus images
---------------
Either images/manifest.csv with columns filename,true_class,true_class_id, or the
standard directory-per-class layout images/<class_name>/*.jpg. See images/README.md.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif"}
GEN_HEADER = ["t_mac", "event_idx", "image_id", "true_class", "true_class_id",
              "patch_contrast", "duration_ms", "is_target"]


# ------------------------------------------------------------------ stimulus

def load_stimulus(images_dir: Path) -> list[dict]:
    """[{path, image_id, true_class, true_class_id}], from a manifest or a tree."""
    manifest = images_dir / "manifest.csv"
    if manifest.exists():
        out = []
        with open(manifest, newline="") as fh:
            for row in csv.DictReader(fh):
                out.append({
                    "path": images_dir / row["filename"],
                    "image_id": row["filename"],
                    "true_class": row["true_class"],
                    "true_class_id": int(row.get("true_class_id", -1)),
                })
        return out

    out = []
    for class_dir in sorted(p for p in images_dir.iterdir() if p.is_dir()):
        for img in sorted(class_dir.iterdir()):
            if img.suffix.lower() in IMG_EXT:
                out.append({
                    "path": img,
                    "image_id": f"{class_dir.name}/{img.name}",
                    "true_class": class_dir.name,
                    "true_class_id": -1,
                })
    return out


# ------------------------------------------------------------------ schedule

def build_schedule(args, stimulus: list[dict]) -> list[dict]:
    """Every event, decided up front, so --dry-run and the real run agree."""
    rng = random.Random(args.seed)
    pool = list(stimulus)
    rng.shuffle(pool)

    def next_image(i: int) -> dict:
        # Images repeat across the matrix. That is fine for a controlled energy
        # benchmark and is noted in the write-up, but shuffle once per run so a
        # given index is not always the same picture.
        return pool[i % len(pool)]

    def dwell() -> float:
        if args.dwell_dist == "fixed":
            return args.mean_interval
        return rng.expovariate(1.0 / args.mean_interval)

    schedule, real_idx = [], 0
    for _ in range(args.n_events):
        # Flicker bait rides in the dwell before the real event.
        if args.flicker_rate > 0 and rng.random() < args.flicker_rate:
            schedule.append({
                "dwell_s": dwell() * rng.random(),
                "kind": "flicker",
                "image": None,
                "contrast": args.flicker_contrast,
                "duration_ms": args.flicker_duration_ms,
            })
        img = next_image(real_idx)
        schedule.append({
            "dwell_s": dwell(),
            "kind": "event",
            "image": img,
            "contrast": args.contrast,
            "duration_ms": args.duration_ms,
        })
        real_idx += 1
    return schedule


def log_row(writer, sink, t: float, idx: int, item: dict, target: str) -> None:
    img = item["image"]
    if img is None:
        row = [f"{t:.3f}", idx, "NONE", "NONE", -1,
               f"{item['contrast']:.3f}", item["duration_ms"], 0]
    else:
        row = [f"{t:.3f}", idx, img["image_id"], img["true_class"],
               img["true_class_id"], f"{item['contrast']:.3f}",
               item["duration_ms"], int(img["true_class"] == target)]
    writer.writerow(row)
    sink.flush()


# -------------------------------------------------------------------- render

def run_display(args, schedule: list[dict], writer, sink) -> None:
    try:
        import pygame
    except ImportError:
        sys.exit("pygame not installed. Run tools/setup_mac.sh, or use --dry-run.")

    pygame.init()
    pygame.mouse.set_visible(False)
    flags = pygame.FULLSCREEN | pygame.SCALED
    screen = pygame.display.set_mode((0, 0), flags, display=args.display, vsync=1)
    pygame.display.set_caption("tiered-wakeup stimulus")
    w, h = screen.get_size()
    print(f"# display {args.display}: {w}x{h}", file=sys.stderr)

    # Trigger patch: a square in a corner, sized as a fraction of the short edge.
    side = int(min(w, h) * args.patch_frac)
    patch = {
        "tl": (0, 0), "tr": (w - side, 0),
        "bl": (0, h - side), "br": (w - side, h - side),
    }[args.patch_corner] + (side, side)

    # Image region: centred, leaving the patch corner clear.
    margin = side + int(min(w, h) * 0.04)
    box = pygame.Rect(margin, margin, w - 2 * margin, h - 2 * margin)

    cache: dict[str, "pygame.Surface"] = {}

    def surface_for(img: dict):
        key = img["image_id"]
        if key not in cache:
            s = pygame.image.load(str(img["path"])).convert()
            sw, sh = s.get_size()
            scale = min(box.width / sw, box.height / sh)
            cache[key] = pygame.transform.smoothscale(
                s, (max(1, int(sw * scale)), max(1, int(sh * scale))))
        return cache[key]

    def draw_black():
        screen.fill((0, 0, 0))
        pygame.display.flip()

    def pump() -> bool:
        for e in pygame.event.get():
            if e.type == pygame.QUIT or (
                    e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE, pygame.K_q)):
                return False
        return True

    draw_black()
    # Let the display settle and the operator get out of the way before the
    # first event; the first flash otherwise lands during window fade-in.
    t_settle = time.time() + args.lead_in
    while time.time() < t_settle:
        if not pump():
            pygame.quit()
            return
        time.sleep(0.01)

    idx = 0
    for item in schedule:
        t_wake = time.time() + item["dwell_s"]
        while time.time() < t_wake:
            if not pump():
                pygame.quit()
                return
            time.sleep(0.005)

        level = int(round(255 * item["contrast"]))
        screen.fill((0, 0, 0))
        if item["image"] is not None:
            surf = surface_for(item["image"])
            screen.blit(surf, surf.get_rect(center=box.center))
        pygame.draw.rect(screen, (level, level, level), patch)
        pygame.display.flip()
        # vsync makes flip() return after the frame is presented, so this is the
        # closest this side gets to a photon timestamp.
        t_on = time.time()

        log_row(writer, sink, t_on, idx, item, args.target_class)
        label = "flicker" if item["image"] is None else item["image"]["image_id"]
        print(f"[{idx:4d}] {time.strftime('%H:%M:%S')} {label} "
              f"c={item['contrast']:.2f} {item['duration_ms']}ms", file=sys.stderr)
        idx += 1

        t_off = t_on + item["duration_ms"] / 1000.0
        while time.time() < t_off:
            if not pump():
                pygame.quit()
                return
            time.sleep(0.005)
        draw_black()

    draw_black()
    time.sleep(args.lead_in)
    pygame.quit()


def run_dry(args, schedule: list[dict], writer, sink) -> None:
    """Same schedule, same log, no window and no pygame."""
    t = time.time()
    for idx, item in enumerate(schedule):
        t += item["dwell_s"]
        log_row(writer, sink, t, idx, item, args.target_class)
        t += item["duration_ms"] / 1000.0
    total = t - time.time()
    print(f"# dry run: {len(schedule)} events, {total / 60:.1f} min of wall clock "
          f"if actually shown", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-events", type=int, default=40, help="real (non-flicker) events")
    ap.add_argument("--mean-interval", type=float, default=45.0,
                    help="mean black dwell, seconds -- this sets the event rate")
    ap.add_argument("--dwell-dist", choices=["exponential", "fixed"],
                    default="exponential",
                    help="exponential unless you are sanity-checking timing")
    ap.add_argument("--duration-ms", type=int, default=15000,
                    help="how long each image is held")
    ap.add_argument("--contrast", type=float, default=0.8,
                    help="trigger-patch luminance, 0..1")
    ap.add_argument("--flicker-rate", type=float, default=0.0,
                    help="probability of a sub-threshold bait flash before each event")
    ap.add_argument("--flicker-contrast", type=float, default=0.15)
    ap.add_argument("--flicker-duration-ms", type=int, default=200)
    ap.add_argument("--target-class", default="banana")
    ap.add_argument("--images", default=str(REPO / "images"))
    ap.add_argument("--display", type=int, default=0)
    ap.add_argument("--patch-corner", choices=["tl", "tr", "bl", "br"], default="tr")
    ap.add_argument("--patch-frac", type=float, default=0.18,
                    help="patch side as a fraction of the screen's short edge")
    ap.add_argument("--lead-in", type=float, default=3.0,
                    help="black seconds before the first and after the last event")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("-o", "--out", default="gen.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="walk the schedule with no window; verifies the log alone")
    args = ap.parse_args()

    if not 0.0 <= args.contrast <= 1.0:
        sys.exit("--contrast must be in 0..1")

    images_dir = Path(args.images)
    stimulus = load_stimulus(images_dir) if images_dir.is_dir() else []
    if not stimulus:
        if not args.dry_run:
            sys.exit(f"no stimulus images under {images_dir} -- see images/README.md")
        print(f"# no images under {images_dir}; dry run uses placeholders",
              file=sys.stderr)
        stimulus = [{"path": None, "image_id": f"placeholder_{i:03d}.jpg",
                     "true_class": "banana" if i % 20 == 0 else f"class_{i % 20}",
                     "true_class_id": -1} for i in range(20)]
    else:
        print(f"# {len(stimulus)} stimulus images, "
              f"{len({s['true_class'] for s in stimulus})} classes", file=sys.stderr)

    schedule = build_schedule(args, stimulus)
    n_flicker = sum(1 for s in schedule if s["kind"] == "flicker")
    print(f"# {args.n_events} events + {n_flicker} flicker, "
          f"mean interval {args.mean_interval:g}s ({args.dwell_dist}), "
          f"duration {args.duration_ms}ms, contrast {args.contrast:g}",
          file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as sink:
        writer = csv.writer(sink)
        writer.writerow(GEN_HEADER)
        if args.dry_run:
            run_dry(args, schedule, writer, sink)
        else:
            run_display(args, schedule, writer, sink)
    print(f"# wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
