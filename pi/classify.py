#!/usr/bin/env python3
"""Tier 3 capture + classify: picamera2 -> MobileNetV2 (INT8 or FP32) via TFLite.

Importable as a library by pi_daemon.py and tools/latency_bench.py, and runnable
on its own to check the camera and the model:

    pi/classify.py --model int8 --show-top 5              # one capture, on the Pi
    pi/classify.py --model int8 --image some.jpg          # no camera, works anywhere
    pi/classify.py --model int8 --benchmark 100           # inference latency only

Downscaling happens in the ISP, not the CPU: the camera is configured to deliver
224x224 directly, so no full-resolution frame is ever copied into Python. On a
Pi 4 that is the difference between a capture that costs single-digit ms and one
that costs tens.

Two things that bite
--------------------
COLOUR ORDER. picamera2's "RGB888" hands back arrays in *BGR* order, and
"BGR888" hands back RGB. Getting it backwards costs several points of top-1 and
looks exactly like camera degradation rather than a bug. --swap-rgb (default on
with RGB888) fixes it; verify once against a known image with
tools/reference_predict.py before trusting any accuracy number.

LABEL OFFSET. The standard quantized MobileNetV2 has 1001 outputs -- ImageNet's
1000 classes plus a "background" class at index 0. If labels.txt has 1000 lines
the indices are off by one and every prediction is silently the neighbouring
class. Both layouts are handled here, but check what you have.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODELS = REPO / "pi" / "models"
MODEL_FILES = {
    "int8": "mobilenet_v2_1.0_224_quant.tflite",
    "fp32": "mobilenet_v2_1.0_224.tflite",
}


def load_interpreter(model_path: Path, num_threads: int = 4):
    """The TFLite runtime moved packages twice; try all three spellings."""
    errors = []
    try:
        from ai_edge_litert.interpreter import Interpreter
        return Interpreter(model_path=str(model_path), num_threads=num_threads)
    except ImportError as e:
        errors.append(f"ai_edge_litert: {e}")
    try:
        from tflite_runtime.interpreter import Interpreter
        return Interpreter(model_path=str(model_path), num_threads=num_threads)
    except ImportError as e:
        errors.append(f"tflite_runtime: {e}")
    try:
        import tensorflow as tf
        return tf.lite.Interpreter(model_path=str(model_path), num_threads=num_threads)
    except ImportError as e:
        errors.append(f"tensorflow: {e}")
    raise ImportError("no TFLite runtime found. Try:\n"
                      "  pip install ai-edge-litert     (or tflite-runtime)\n"
                      + "\n".join("  " + e for e in errors))


def load_labels(path: Path) -> list[str]:
    with open(path) as fh:
        labels = [ln.strip() for ln in fh if ln.strip()]
    # Some label files are "0  tench, Tinca tinca" -- keep the readable part.
    cleaned = []
    for ln in labels:
        parts = ln.split(None, 1)
        cleaned.append(parts[1] if len(parts) == 2 and parts[0].isdigit() else ln)
    return cleaned


class Classifier:
    """MobileNetV2 on TFLite, INT8 or FP32, with the quantization handled."""

    def __init__(self, model: str = "int8", models_dir: Path = MODELS,
                 num_threads: int = 4, labels_path: Path | None = None):
        import numpy as np
        self.np = np
        self.model_name = model
        self.model_path = models_dir / MODEL_FILES[model]
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"{self.model_path} missing -- run pi/models/fetch_models.sh")

        self.interpreter = load_interpreter(self.model_path, num_threads)
        self.interpreter.allocate_tensors()
        self.inp = self.interpreter.get_input_details()[0]
        self.out = self.interpreter.get_output_details()[0]
        _, self.height, self.width, _ = self.inp["shape"]
        self.n_classes = int(self.out["shape"][-1])

        labels_path = labels_path or (models_dir / "labels.txt")
        self.labels = load_labels(labels_path) if labels_path.exists() else []
        # 1001 outputs = 1000 ImageNet classes + "background" at index 0.
        self.label_offset = 0
        if self.labels and len(self.labels) == self.n_classes - 1:
            self.label_offset = 1

    def label(self, class_id: int) -> str:
        i = class_id - self.label_offset
        if self.labels and 0 <= i < len(self.labels):
            return self.labels[i]
        return f"class_{class_id}"

    def class_id_for(self, name: str) -> int:
        """Resolve a target class by name, so no index is ever hardcoded."""
        want = name.strip().lower()
        for i, lab in enumerate(self.labels):
            if want == lab.lower() or want in [p.strip().lower()
                                               for p in lab.split(",")]:
                return i + self.label_offset
        raise KeyError(f"{name!r} not in labels.txt ({len(self.labels)} entries)")

    def preprocess(self, rgb):
        """HxWx3 uint8 RGB -> the tensor this model wants."""
        np = self.np
        arr = np.asarray(rgb)
        if arr.shape[:2] != (self.height, self.width):
            raise ValueError(
                f"got {arr.shape[:2]}, model wants {(self.height, self.width)}. "
                "Configure the camera to deliver this size; do not resize in Python.")
        if self.inp["dtype"] == np.uint8:
            return arr.astype(np.uint8)[None]
        # FP32 MobileNetV2 preprocessing is x/127.5 - 1, not x/255.
        return (arr.astype(np.float32) / 127.5 - 1.0)[None]

    def infer(self, rgb, top_k: int = 5):
        """-> (class_id, confidence, [(id, conf)...], infer_ms)"""
        np = self.np
        tensor = self.preprocess(rgb)
        t0 = time.perf_counter()
        self.interpreter.set_tensor(self.inp["index"], tensor)
        self.interpreter.invoke()
        raw = self.interpreter.get_tensor(self.out["index"])[0]
        infer_ms = (time.perf_counter() - t0) * 1000.0

        scale, zero = self.out.get("quantization", (0.0, 0))
        probs = ((raw.astype(np.float32) - zero) * scale
                 if scale else raw.astype(np.float32))
        total = float(probs.sum())
        # Quantized softmax output sums to ~1 already; a logit head does not.
        if not 0.9 <= total <= 1.1:
            shifted = probs - probs.max()
            exp = np.exp(shifted)
            probs = exp / exp.sum()

        order = np.argsort(probs)[::-1][:top_k]
        top = [(int(i), float(probs[i])) for i in order]
        return top[0][0], top[0][1], top, infer_ms


class Camera:
    """picamera2 delivering 224x224 straight out of the ISP."""

    def __init__(self, width: int = 224, height: int = 224,
                 swap_rgb: bool = True, fmt: str = "RGB888"):
        from picamera2 import Picamera2
        self.swap_rgb = swap_rgb
        self.picam = Picamera2()
        config = self.picam.create_still_configuration(
            main={"size": (width, height), "format": fmt},
            buffer_count=2)
        self.picam.configure(config)
        # Fixed exposure and gain, or auto-exposure hunting between a black dwell
        # and a bright flash adds hundreds of ms of variance to every capture and
        # changes what the model sees between otherwise identical events.
        self.picam.set_controls({"AeEnable": False, "AwbEnable": False,
                                 "ExposureTime": 8000, "AnalogueGain": 2.0})
        self.picam.start()
        time.sleep(0.5)   # let the sensor settle before the first frame

    def capture(self):
        """-> (HxWx3 uint8 RGB, capture_ms)"""
        t0 = time.perf_counter()
        arr = self.picam.capture_array("main")
        if arr.shape[2] == 4:
            arr = arr[:, :, :3]
        if self.swap_rgb:
            arr = arr[:, :, ::-1]      # picamera2 "RGB888" is really BGR
        return arr, (time.perf_counter() - t0) * 1000.0

    def close(self):
        try:
            self.picam.stop()
            self.picam.close()
        except Exception:
            pass


def load_image_file(path: Path, size: int = 224):
    """Centre-crop then resize, matching tools/reference_predict.py exactly."""
    import numpy as np
    from PIL import Image
    img = Image.open(path).convert("RGB")
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2,
                    (w + side) // 2, (h + side) // 2))
    return np.asarray(img.resize((size, size), Image.BILINEAR), dtype=np.uint8)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", choices=["int8", "fp32"], default="int8")
    ap.add_argument("--models-dir", type=Path, default=MODELS)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--image", type=Path, help="classify a file instead of a capture")
    ap.add_argument("--show-top", type=int, default=5)
    ap.add_argument("--benchmark", type=int, metavar="N",
                    help="time N inferences on one frame and exit")
    ap.add_argument("--target-class", default="banana",
                    help="resolve this label to an index and print it")
    ap.add_argument("--swap-rgb", dest="swap_rgb", action="store_true", default=True)
    ap.add_argument("--no-swap-rgb", dest="swap_rgb", action="store_false")
    args = ap.parse_args()

    clf = Classifier(args.model, args.models_dir, args.threads)
    print(f"model  {clf.model_path.name}")
    print(f"input  {clf.inp['shape'].tolist()} {clf.inp['dtype'].__name__}")
    print(f"output {clf.n_classes} classes, label offset {clf.label_offset}")
    try:
        tid = clf.class_id_for(args.target_class)
        print(f"target {args.target_class!r} -> class_id {tid} ({clf.label(tid)})")
    except KeyError as e:
        print(f"target {e}")

    if args.image:
        frame, cap_ms = load_image_file(args.image, clf.width), 0.0
    else:
        cam = Camera(clf.width, clf.height, args.swap_rgb)
        frame, cap_ms = cam.capture()

    if args.benchmark:
        times = []
        for _ in range(args.benchmark):
            times.append(clf.infer(frame)[3])
        times.sort()
        n = len(times)
        print(f"\ninference over {n} runs, ms:")
        print(f"  min {times[0]:.1f}  p50 {times[n // 2]:.1f}  "
              f"p95 {times[int(n * 0.95)]:.1f}  max {times[-1]:.1f}  "
              f"mean {sum(times) / n:.1f}")
        return 0

    cid, conf, top, ms = clf.infer(frame, args.show_top)
    print(f"\ncapture {cap_ms:.1f} ms, inference {ms:.1f} ms")
    for i, (c, p) in enumerate(top):
        print(f"  {i + 1}. {clf.label(c):<40} {p:.3f}  (id {c})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
