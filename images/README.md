# `images/` — the stimulus set

ImageNet validation JPEGs that `tools/event_display.py` flashes on the monitor and Tier 2
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
ILSVRC2012_val_00012345.JPEG,banana,954
ILSVRC2012_val_00067890.JPEG,coffee_mug,504
```

**Use this one.** It is the only layout that carries real ImageNet class *indices*, and the index
is what `reference_predict.py` scores predictions against. Class 954 is `banana`.

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

## Balance

A few hundred images across ~20 classes, roughly 20% target class (`banana`, id 954). Enough
positives that detection rate has resolution at 40 events per matrix cell; few enough that a
detector which always answers "banana" still scores badly.

Images repeat across the matrix. That is fine for a controlled *energy* benchmark — the same
stimulus under different power policies is the point — and it gets noted in the writeup rather
than hidden.

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

- Monitor at **100% brightness** for every run. Lower settings use backlight PWM, which Tier 0's
  30–50 Hz low-pass may not fully reject.
- Camera perpendicular to the screen, fixed exposure, slight defocus to kill moiré.
- The trigger patch is drawn by `event_display.py`, not stored here. Its contrast is a swept
  parameter and deliberately independent of image content — that is why Tier 0's ROC is not
  confounded by whether a given photo happens to be a dark night scene.
