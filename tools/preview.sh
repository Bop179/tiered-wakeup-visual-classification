#!/bin/sh
# Live Pi camera preview in the Mac's browser. Ctrl-C stops it and frees the camera.
#   tools/preview.sh           daemon's fixed exposure (what the model sees)
#   tools/preview.sh --auto    auto exposure, easier for aiming in a dark room
(sleep 3; open http://localhost:8000) &
exec ssh -t -L 8000:localhost:8000 "${PI_HOST:-pi}" \
  "python3 ~/tiered-wakeup-visual-classification/pi/preview.py $*"
