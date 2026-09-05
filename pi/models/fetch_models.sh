#!/usr/bin/env bash
# Fetch MobileNetV2 INT8 + FP32 and the ImageNet labels. Run once, on the Pi and
# on the Mac. Models are gitignored -- they are large and they are not ours.
set -euo pipefail
cd "$(dirname "$0")"

BASE="https://storage.googleapis.com/download.tensorflow.org/models/tflite_11_05_08"

fetch() {   # url, output tarball, expected .tflite inside
  local url="$1" tgz="$2" want="$3"
  if [ -f "$want" ]; then echo "have $want"; return; fi
  echo "fetching $url"
  curl -fL --retry 3 -o "$tgz" "$url"
  tar xzf "$tgz"
  rm -f "$tgz"
  [ -f "$want" ] || { echo "expected $want inside $tgz" >&2; exit 1; }
}

fetch "$BASE/mobilenet_v2_1.0_224_quant.tgz" q.tgz mobilenet_v2_1.0_224_quant.tflite
fetch "$BASE/mobilenet_v2_1.0_224.tgz"       f.tgz mobilenet_v2_1.0_224.tflite

if [ ! -f labels.txt ]; then
  # The archives ship labels as labels.txt or as a *_labels.txt; take whichever.
  found=$(ls -1 *labels*.txt 2>/dev/null | head -1 || true)
  if [ -n "$found" ] && [ "$found" != "labels.txt" ]; then
    mv "$found" labels.txt
  else
    curl -fL --retry 3 -o labels.txt \
      "https://raw.githubusercontent.com/google-coral/test_data/master/imagenet_labels.txt"
  fi
fi

echo
wc -l < labels.txt | xargs echo "labels.txt lines:"
echo "NOTE: 1001 lines means index 0 is 'background' and the model's 1001 outputs"
echo "map 1:1. 1000 lines means indices are offset by one -- pi/classify.py"
echo "detects and handles both, but check which you have."
ls -lh ./*.tflite
sha256sum ./*.tflite 2>/dev/null || shasum -a 256 ./*.tflite
