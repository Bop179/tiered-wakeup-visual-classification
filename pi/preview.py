"""Live camera preview as MJPEG over HTTP, for aiming and focusing.

Run from the Mac with tools/preview.sh, which tunnels it to http://localhost:8000.
Holds the camera, so it refuses while tier3-daemon is running.
"""
import io
import subprocess
import sys
import threading
from http import server
from socketserver import ThreadingMixIn

from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

PAGE = b"""<html><head><title>Pi camera</title></head>
<body style="margin:0;background:#111">
<img src="/stream.mjpg" style="display:block;width:100vw;height:100vh;object-fit:contain">
</body></html>"""


class Frames(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.cond = threading.Condition()

    def write(self, buf):
        with self.cond:
            self.frame = buf
            self.cond.notify_all()


class Handler(server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/stream.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()
        try:
            while True:
                with frames.cond:
                    frames.cond.wait()
                    frame = frames.frame
                self.wfile.write(b"--FRAME\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser tab closed

    def log_message(self, *args):
        pass


class Server(ThreadingMixIn, server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    if subprocess.run(["systemctl", "is-active", "--quiet", "tier3-daemon"]).returncode == 0:
        sys.exit("tier3-daemon is running and owns the camera; stop it first")
    frames = Frames()
    cam = Picamera2()
    cam.configure(cam.create_video_configuration(main={"size": (1014, 760)}))
    if "--auto" not in sys.argv:
        # Same fixed exposure as classify.Camera, so brightness matches what the model sees
        cam.set_controls({"AeEnable": False, "AwbEnable": False,
                          "ExposureTime": 8000, "AnalogueGain": 2.0})
    cam.start_recording(MJPEGEncoder(), FileOutput(frames))
    try:
        print("preview on :8000, Ctrl-C to stop", flush=True)
        Server(("127.0.0.1", 8000), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        cam.stop_recording()
