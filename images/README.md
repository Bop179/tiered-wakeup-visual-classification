# `images/` — the stimulus set

ImageNet validation JPEGs that `tools/event_display.py` flashes on the monitor and Tier 3
classifies. **Gitignored** — a few hundred JPEGs are not repo contents, and ImageNet is not ours
to redistribute.

## Layout

`load_stimulus()` in `tools/event_display.py` accepts two layouts, and it checks for the manifest
first. `tools/reference_predict.py` imports the same function, so both always see the same set.

### Preferred: `manifest.csv`

```
images/manifest.csv
images/whatever_filenames_you_like.JPEG
```

```csv
filename,true_class,true_class_id
ILSVRC2012_val_00012345.JPEG,banana,955
ILSVRC2012_val_00067890.JPEG,coffee_mug,505
```

**Use this one.** It is the only layout that carries real ImageNet class *indices*, and the index
is what `reference_predict.py` scores predictions against.

### The index is 955, not 954 — read this before authoring the manifest

**There are two ImageNet index conventions and they differ by one.** Almost every reference you
will find online — torchvision, the usual class-list gists, most papers — uses the **1000-class**
space where `banana` is **954** and `coffee mug` is **504**.

This model does not use that space. `mobilenet_v2_1.0_224_quant.tflite` has **1001 outputs**:
ImageNet's 1000 classes plus a `background` class at index 0. Everything is therefore shifted up
by one, and `true_class_id` in the manifest must be in **the model's space**:

| Class | 1000-class space (what you'll find online) | **This model — use this** |
|---|---|---|
| `banana` | 954 | **955** |
| `coffee mug` | 504 | **505** |

Getting this wrong is silent and expensive. `reference_predict.py` scores with raw integer
equality (`pred_id == true_class_id`), so an off-by-one manifest marks **every single image
wrong** and reports a top-1 near 0%. It looks exactly like a catastrophic capture-path failure —
glare, moiré, defocus — which is the one thing this whole reference step exists to rule out.
`--swap-check` does not catch it either, because the swapped run is off by one too.

**Never type an index by hand.** Ask the model what it calls the class:

```bash
.venv/bin/python pi/classify.py --target-class banana --resolve-only
# target 'banana' -> class_id 955 (banana)
```

`reference_predict.py` also cross-checks every manifest row's `true_class_id` against the name in
the same row and refuses to run on a mismatch, so a bad manifest fails loudly at startup instead
of quietly producing a wrong accuracy ceiling.

### Fallback: one directory per class

```
images/banana/ILSVRC2012_val_00012345.JPEG
images/coffee_mug/ILSVRC2012_val_00067890.JPEG
```

Class name comes from the directory name; `image_id` becomes `banana/ILSVRC2012_val_00012345.JPEG`.
Every `true_class_id` is set to **-1**, meaning "no index available", and `reference_predict.py`
cannot score top-1 correctness against it. Fine for a quick smoke test, not for the accuracy
numbers. Loose files sitting directly in `images/` with no manifest are **ignored entirely** —
only subdirectories are walked.

Recognised extensions: `.jpg .jpeg .png .bmp .gif`, case-insensitive, so `.JPEG` is fine.

## How to build it

```bash
.venv/bin/python tools/fetch_stimulus.py            # --dry-run to check first
```

Pulls real ILSVRC-2012 validation JPEGs from the `evanarlian/imagenet_1k_resized_256` mirror on
the HuggingFace datasets server — the official `imagenet-1k` repo is gated, this one is not.
Images come resized to 256 on the short side: large enough to fill a monitor region for the
camera, small enough that the whole set is ~5 MB. Re-running skips files already on disk, so a
partial download is resumed rather than repeated.

The script writes `manifest.csv` itself, and **resolves every class through
`classify.class_id_for()` rather than trusting arithmetic.** The mirror labels in the 1000-class
space, the model needs 1001, and a build where any class disagrees aborts before a single image
is downloaded. That is the guard described above, applied at the point the indices are created
rather than only where they are consumed.

## Balance

275 images across 25 classes, 50 of them `banana` (18.2%). Enough positives that detection rate
has resolution at 40 events per matrix cell; few enough that a detector which always answers
"banana" scores 18%.

The 24 non-target classes are chosen, not sampled, and split into two groups:

| Group | Classes | Why |
|---|---|---|
| **hard** (13) | lemon, orange, pineapple, Granny Smith, fig, spaghetti/butternut squash, zucchini, cucumber, bell pepper, ear of corn, hotdog, French loaf | Yellow, elongated or produce-shaped — the things a banana detector actually trips on. These are what give the false-positive rate any meaning. |
| **easy** (11) | golden retriever, sports car, coffee mug, laptop, acoustic guitar, park bench, traffic light, umbrella, teapot, backpack, analog clock, barn | No relationship to bananas at all. Most of a real-world stream looks like this, and a detector that fires on them is broken in an obvious way. |

"Hard" and "easy" mean *confusable with a banana*, *not* hard or easy to classify — the easy
group actually scores lower, because umbrellas and laptops are genuinely difficult ImageNet
classes while produce is visually distinctive.

`corn` is deliberately absent. ImageNet splits corn across `corn` and `ear, spike, capitulum`,
the model sends real corn images to the latter, and carrying both would have scored a taxonomy
quirk as vision error — it measured 0/9 with 6 of the 9 going to the neighbouring class.

Images repeat across the matrix. That is fine for a controlled *energy* benchmark — the same
stimulus under different power policies is the point — and it gets noted in the writeup rather
than hidden.

## The measured ceiling, 2026-09-06

From `tools/reference_predict.py --images images -o data/reference.csv --swap-check`, on
`mobilenet_v2_1.0_224_quant.tflite`, no camera in the loop:

```
top-1                     177/275 = 64.4%     <- the ceiling
  target (banana)          32/50  = 64.0%
  hard negatives           85/117 = 72.6%
  easy negatives           60/108 = 55.6%
banana recall              32/50  = 0.64
banana false positives      1/225              <- a fig, at 0.52 confidence
channel-swap agreement     44.0%               <- low is healthy
```

**Anything below 64.4% on the Pi is capture degradation, not model error.** The single false
positive being a fig rather than a golden retriever is the distractor set working as intended.
Re-measure and update this block whenever the stimulus set changes.

## Before trusting any accuracy number

Run the model on these files offline first:

```bash
.venv/bin/python tools/reference_predict.py --images images/ -o data/reference.csv
```

That records what MobileNetV2 answers with **no camera in the loop**. Every on-Pi result is scored
against it, which separates *model error* (the model was always going to get this wrong) from
*screen-capture degradation* (glare, moiré, exposure, defocus). Without it, a disappointing
accuracy number has two possible causes and no way to tell them apart.

`--swap-check` re-scores with R and B swapped. If that scores *better*, the on-Pi channel order is
wrong — a bug worth catching on the Mac in one CPU-minute rather than on the Pi during build week.

## Display notes

- Monitor at **100% brightness** for every run. Lower settings use backlight PWM, which Tier 1's
  30–50 Hz low-pass may not fully reject.
- Camera perpendicular to the screen, fixed exposure, slight defocus to kill moiré.
- The trigger patch is drawn by `event_display.py`, not stored here. Its contrast is a swept
  parameter and deliberately independent of image content — that is why Tier 1's ROC is not
  confounded by whether a given photo happens to be a dark night scene.
