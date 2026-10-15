#!/usr/bin/env python3
"""Conductor: drive Hyprland with hand gestures seen by the webcam.

Every frame goes through MediaPipe's hand tracker, locally; no image leaves
this process or touches the disk. The hand's shape is read from the 21 joint
positions it reports, which hold up far better in dim light and at odd angles
than MediaPipe's own gesture classifier. Nothing happens until you arm it by
holding up an open palm. While armed, one gesture fires an action and
disarms again, so bringing your hand back after a swipe can't trigger the
opposite swipe:

  swipe left / right   previous / next workspace
  swipe up             toggle fullscreen
  thumb up (hold)      play / pause
  victory (hold)       next track

Directions are as you see them: the camera image is mirrored first. Swipe
down is off by default, since it's also how you put your hand down.

Actions can be overridden in ~/.config/omarchy/conductor.json, e.g.
  {"actions": {"swipe-down": "", "victory": "omarchy-shell media previous"}}
A value starting with "hl." is a Hyprland dispatcher, anything else runs as a
shell command, and "" turns that gesture off. To use swipe down, give it an
action, such as 'hl.dsp.workspace.toggle_special("scratchpad")'.

--camera infrared uses the laptop's infrared (face login) camera instead,
found as the video device that delivers greyscale frames, and switches on
its infrared light, so it sees as well in the dark as in daylight; the
colour camera needs a lit room. The choice can also go in conductor.json as
{"camera": "infrared"}.

The light is switched on through Microsoft's standard UVC extension for
face-login cameras (see IR_* below), which most Windows Hello cameras have.
In face-login mode the camera lights every other frame, so the unlit ones
are skipped: about 7.5 usable frames a second.

The camera is released (and its light goes off) while the screen is locked.
Progress is reported to the Omarchy shell as JSON lines on stdout.
"""

import argparse
import ctypes
import fcntl
import struct
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections import deque

os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import cv2  # noqa: E402
import mediapipe as mp  # noqa: E402
from mediapipe.tasks.python import BaseOptions, vision  # noqa: E402

DEFAULT_ACTIONS = {
    "swipe-left": 'hl.dsp.focus({ workspace = "e-1" })',
    "swipe-right": 'hl.dsp.focus({ workspace = "e+1" })',
    "swipe-up": 'hl.dsp.window.fullscreen({ mode = "fullscreen" })',
    "swipe-down": "",
    "thumb-up": "omarchy-shell media playPause",
    "victory": "omarchy-shell media next",
}

# Hand shapes (see hand_pose) that fire when held.
POSES = {"thumb-up": "thumb-up", "victory": "victory"}

ARM_HOLD = 0.3        # seconds of open palm to arm
POSE_HOLD = 0.45      # seconds a hold gesture must be held
POSE_SHARE = 0.6      # share of frames in a hold that must show the pose;
                      # the shape reading flickers, especially in dim light
ARMED_TIMEOUT = 4.0   # disarm if nothing happens for this long
LOST_GRACE = 0.4      # tolerate the hand vanishing (motion blur) this long
SWIPE_WINDOW = 0.6    # a swipe has to happen within this many seconds
SWIPE_X = 0.10        # ...and cover this share of the frame width
SWIPE_Y = 0.12        # ...or of its height
EXIT_SWIPE = 0.07     # a fast hand that leaves the frame (or blurs out) only
                      # needs to have moved this far, sideways: dropping out
                      # of view at the bottom is just putting your hand down
MIN_GAIN = 4          # camera gain while running, if it was lower

# Microsoft's UVC 1.5 extension for face-login cameras. Control 6 (face
# authentication) picks the stream mode; mode 2 lights alternate frames with
# the infrared emitter. Values are 9 bytes: 01 03 <mode> 00 ...
IR_XU_GUID = uuid.UUID("0f3f95dc-2632-4c4e-92c9-a04782f43bc8").bytes_le
IR_SELECTOR = 6
IR_LIT_MODE = 2
IR_DARK = 12          # frames darker than this are the unlit half
IR_BLUR = 5 / 360     # blur, as a share of the frame height: under its own
                      # light a near hand burns out to a flat white shape, and
                      # the tracker only finds it once the edges are softened
UVCIOC_CTRL_QUERY = 0xC0107521
UVC_SET_CUR, UVC_GET_CUR, UVC_GET_MAX = 0x01, 0x81, 0x83
IDLE_AFTER = 20.0     # no hand for this long: drop to IDLE_FPS
IDLE_FPS = 5
LOCK_POLL = 1.0


def emit(**event):
    print(json.dumps(event), flush=True)


def load_config(path):
    try:
        with open(path) as f:
            config = json.load(f)
        if isinstance(config, dict):
            return config
        raise ValueError("not a JSON object")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        print(f"conductor: ignoring {path}: {e}", file=sys.stderr)
    return {}


def load_actions(config):
    actions = dict(DEFAULT_ACTIONS)
    user = config.get("actions", {})
    if isinstance(user, dict):
        actions.update({k: str(v) for k, v in user.items() if k in DEFAULT_ACTIONS})
    return actions


def find_infrared():
    """The first video device that delivers 8-bit greyscale frames: on
    laptops, that's the infrared camera used for face login."""
    for name in sorted(os.listdir("/dev"), key=lambda n: (len(n), n)):
        if not name.startswith("video"):
            continue
        try:
            formats = subprocess.run(["v4l2-ctl", "-d", f"/dev/{name}", "--list-formats"],
                                     capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        if "'GREY'" in formats:
            return int(name[5:])
    return None


def run(action, dry_run):
    if not action:
        return
    if dry_run:
        return
    if action.startswith("hl."):
        cmd = ["hyprctl", "dispatch", action]
    else:
        cmd = ["sh", "-c", action]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def screen_locked():
    return subprocess.run(["pgrep", "-x", "hyprlock"], stdout=subprocess.DEVNULL).returncode == 0


def dist(a, b):
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5


def hand_pose(lm, image_lm):
    """Name the hand's shape: "palm", "thumb-up", "victory", "fist" or None.

    lm are the joints in 3D (metres, around the hand's centre), so a finger
    pointing a little towards the camera still reads as straight. A finger is
    out when its tip is well past its middle joint, measured from the wrist,
    so it works at any size or angle. "Up" for the thumb comes from the image
    (image_lm), where y grows downwards."""
    wrist = lm[0]

    def out(tip, pip):
        return dist(wrist, lm[tip]) > 1.1 * dist(wrist, lm[pip])

    def curled(tip, pip):
        return dist(wrist, lm[tip]) < 1.0 * dist(wrist, lm[pip])

    fingers = [(8, 6), (12, 10), (16, 14), (20, 18)]  # index .. pinky
    extended = [out(*f) for f in fingers]
    folded = [curled(*f) for f in fingers]
    palm_size = dist(wrist, lm[9])
    thumb_out = dist(lm[4], lm[5]) > 0.6 * palm_size

    if all(extended):
        return "palm"
    if extended[0] and extended[1] and folded[2] and folded[3]:
        return "victory"
    image_palm = ((image_lm[0].x - image_lm[9].x) ** 2 + (image_lm[0].y - image_lm[9].y) ** 2) ** 0.5
    if all(folded) and thumb_out and image_lm[4].y < image_lm[5].y - 0.25 * image_palm:
        return "thumb-up"
    if all(folded):
        return "fist"
    return None


def palm_center(landmarks):
    # Wrist and the four knuckles: steadier than any fingertip. Mirrored, so
    # moving your hand to your right moves it right here too.
    pts = [landmarks[i] for i in (0, 5, 9, 13, 17)]
    return 1 - sum(p.x for p in pts) / 5, sum(p.y for p in pts) / 5


def xu_query(fd, unit, selector, query, data):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    arg = struct.pack("BBBxHxxQ", unit, selector, query, len(data), ctypes.addressof(buf))
    fcntl.ioctl(fd, UVCIOC_CTRL_QUERY, bytearray(arg))
    return buf.raw


def ir_unit(device):
    """The extension unit ID carrying Microsoft's face-login controls on this
    camera, read from its USB descriptors, or None."""
    sysfs = os.path.realpath(f"/sys/class/video4linux/video{device}/device")
    try:
        interface = int(open(os.path.join(sysfs, "bInterfaceNumber")).read(), 16)
        raw = open(os.path.join(os.path.dirname(sysfs), "descriptors"), "rb").read()
    except (OSError, ValueError):
        return None
    i, current = 0, None
    while i + 1 < len(raw) and raw[i]:
        d = raw[i:i + raw[i]]
        if d[1] == 4:  # interface descriptor
            current = d[2]
        elif d[1] == 0x24 and current == interface and len(d) > 20 and d[2] == 6:
            if d[4:20] == IR_XU_GUID:  # VC extension unit
                return d[3]
        i += raw[i]
    return None


class Camera:
    def __init__(self, device, infrared=False):
        self.device = device
        self.infrared = infrared
        self.cap = None
        self.saved = {}
        self.saved_ir = None  # (unit, original value) while the emitter is on

    def v4l2(self, *args):
        try:
            return subprocess.run(["v4l2-ctl", "-d", f"/dev/video{self.device}", *args],
                                  capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def control(self, name):
        got = self.v4l2("-C", name)
        if got and got.returncode == 0 and ":" in got.stdout:
            return got.stdout.split(":")[1].strip()
        return None

    def tune(self):
        # In dim light the camera stretches its exposure and drops to ~15 fps,
        # which blurs a swipe into nothing. Hold it at 30 fps and make up the
        # light with gain while we run; settings go back when we let go.
        dynamic = self.control("exposure_dynamic_framerate")
        if dynamic is not None:
            self.saved["exposure_dynamic_framerate"] = dynamic
            self.v4l2("-c", "exposure_dynamic_framerate=0")
        gain = self.control("gain")
        if gain is not None and gain.lstrip("-").isdigit() and int(gain) < MIN_GAIN:
            self.saved["gain"] = gain
            self.v4l2("-c", f"gain={MIN_GAIN}")

    def open(self):
        if self.cap is None:
            cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            if self.infrared:
                # Greyscale frames as they come; the camera lights the scene
                # itself, so there's nothing to tune.
                cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
            else:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                cap.set(cv2.CAP_PROP_FPS, 30)
            if not cap.isOpened():
                cap.release()
                return False
            self.cap = cap
            if self.infrared:
                self.light_on()
            else:
                self.tune()
        return True

    def light_on(self):
        unit = ir_unit(self.device)
        if unit is None:
            print("conductor: no infrared light control found; using ambient light",
                  file=sys.stderr)
            return
        try:
            fd = os.open(f"/dev/video{self.device}", os.O_RDWR)
            try:
                current = xu_query(fd, unit, IR_SELECTOR, UVC_GET_CUR, [0] * 9)
                most = xu_query(fd, unit, IR_SELECTOR, UVC_GET_MAX, [0] * 9)
                if most[2] >= IR_LIT_MODE:
                    lit = bytes([current[0], current[1], IR_LIT_MODE]) + current[3:]
                    xu_query(fd, unit, IR_SELECTOR, UVC_SET_CUR, lit)
                    self.saved_ir = (unit, current)
            finally:
                os.close(fd)
        except OSError as e:
            print(f"conductor: couldn't switch the infrared light on: {e}", file=sys.stderr)

    def light_off(self):
        if self.saved_ir is None:
            return
        unit, value = self.saved_ir
        self.saved_ir = None
        try:
            fd = os.open(f"/dev/video{self.device}", os.O_RDWR)
            try:
                xu_query(fd, unit, IR_SELECTOR, UVC_SET_CUR, value)
            finally:
                os.close(fd)
        except OSError:
            pass

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            return None
        if frame.ndim == 2:
            if self.saved_ir and frame.mean() < IR_DARK:
                return self.read()  # the unlit half of the strobe
            frame = cv2.GaussianBlur(frame, (0, 0), IR_BLUR * frame.shape[0])
            frame = cv2.normalize(frame, None, 0, 255, cv2.NORM_MINMAX)
            frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
        return frame

    def close(self):
        self.light_off()
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        for name, value in self.saved.items():
            self.v4l2("-c", f"{name}={value}")
        self.saved = {}


class Conductor:
    def __init__(self, actions, dry_run):
        self.actions = actions
        self.dry_run = dry_run
        self.state = None
        self.armed_at = 0.0
        self.last_seen = 0.0
        self.track = deque()
        self.lost_since = None
        self.poses = deque()  # (time, pose) for recent frames

    def set_state(self, state):
        if state != self.state:
            self.state = state
            emit(event="state", state=state)

    def fire(self, gesture):
        action = self.actions.get(gesture, "")
        emit(event="gesture", gesture=gesture, action=action)
        run(action, self.dry_run)
        self.disarm()

    def disarm(self):
        self.armed_at = 0.0
        self.track.clear()
        self.poses.clear()

    def held(self, now, pose, seconds):
        """Whether `pose` has dominated the last `seconds`, which must be
        fully covered by frames seen since the pose was first spotted."""
        recent = [p for t, p in self.poses if now - t <= seconds]
        if len(recent) < 3 or now - self.poses[0][0] < seconds * 0.9:
            return False
        return recent.count(pose) >= POSE_SHARE * len(recent)

    def swipe(self, now, threshold_x, threshold_y=None):
        """The swipe the palm made within SWIPE_WINDOW, if it went far enough."""
        recent = [p for p in self.track if now - p[0] <= SWIPE_WINDOW]
        if len(recent) < 2:
            return None
        (_, x0, y0), (_, x, y) = recent[0], recent[-1]
        dx, dy = x - x0, y - y0
        gesture = None
        if abs(dx) >= threshold_x and abs(dx) > 1.5 * abs(dy):
            gesture = "swipe-right" if dx > 0 else "swipe-left"
        elif threshold_y is not None and abs(dy) >= threshold_y and abs(dy) > 1.5 * abs(dx):
            gesture = "swipe-down" if dy > 0 else "swipe-up"
        # A gesture with no action isn't watched for at all, so it can't use
        # up the arming meant for another one.
        return gesture if self.actions.get(gesture) else None

    def update(self, now, pose, landmarks):
        if landmarks is None:
            if self.armed_at and self.lost_since is None:
                # A quick swipe often ends with the hand blurring out or
                # leaving the frame: judge it on the way it was heading.
                self.lost_since = now
                gesture = self.swipe(now, EXIT_SWIPE)
                if gesture:
                    return self.fire(gesture)
            if now - self.last_seen > LOST_GRACE:
                self.disarm()
                self.set_state("watching")
            return
        self.last_seen = now
        self.lost_since = None
        self.poses.append((now, pose))
        while self.poses and now - self.poses[0][0] > max(ARM_HOLD, POSE_HOLD) + 0.1:
            self.poses.popleft()

        if not self.armed_at:
            if self.held(now, "palm", ARM_HOLD):
                self.armed_at = now
                self.track.clear()
                self.poses.clear()
            self.set_state("armed" if self.armed_at else "hand")
            return

        if now - self.armed_at > ARMED_TIMEOUT:
            self.disarm()
            self.set_state("hand")
            return
        self.set_state("armed")

        # Swipes: how far the palm moved within the last SWIPE_WINDOW.
        x, y = palm_center(landmarks)
        self.track.append((now, x, y))
        while self.track and now - self.track[0][0] > SWIPE_WINDOW:
            self.track.popleft()
        # Making a sign shifts the hand a little; that's not a swipe.
        if pose not in POSES:
            gesture = self.swipe(now, SWIPE_X, SWIPE_Y)
            if gesture:
                return self.fire(gesture)

        # Holds: mostly the same pose for POSE_HOLD.
        for shape, gesture in POSES.items():
            if self.actions.get(gesture) and self.held(now, shape, POSE_HOLD):
                return self.fire(gesture)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "omarchy-conductor")
    config = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                          "omarchy", "conductor.json")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--camera", choices=["color", "infrared"],
                   help="which camera to use (default: conductor.json's \"camera\", else color)")
    p.add_argument("--device", type=int, help="video device number, overriding --camera")
    p.add_argument("--model", default=os.path.join(data, "gesture_recognizer.task"))
    p.add_argument("--config", default=config)
    p.add_argument("--dry-run", action="store_true", help="report gestures without acting")
    p.add_argument("--debug", action="store_true", help="print what is seen in every frame to stderr")
    args = p.parse_args()

    config = load_config(args.config)
    infrared = (args.camera or config.get("camera")) == "infrared"
    if args.device is None:
        args.device = find_infrared() if infrared else 0
        if args.device is None:
            emit(event="error", message="no infrared camera found")
            return 1

    if not os.path.exists(args.model):
        emit(event="error", message=f"model missing: run {os.path.join(here, '..', 'tools', 'setup.sh')}")
        return 1

    recognizer = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
        base_options=BaseOptions(model_asset_path=args.model),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.5,
        min_hand_presence_confidence=0.4,
        min_tracking_confidence=0.5,
    ))
    camera = Camera(args.device, infrared)
    # The shell stops us with SIGTERM: exit through `finally` so the camera's
    # settings are put back.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    conductor = Conductor(load_actions(config), args.dry_run)
    start = time.monotonic()
    last_ts = -1
    lock_checked = 0.0
    locked = False

    try:
        while True:
            now = time.monotonic()
            if now - lock_checked >= LOCK_POLL:
                lock_checked = now
                locked = screen_locked()
            if locked:
                camera.close()
                conductor.disarm()
                conductor.set_state("paused")
                time.sleep(LOCK_POLL)
                continue
            if not camera.open():
                emit(event="error", message=f"cannot open /dev/video{args.device}")
                time.sleep(3)
                continue
            frame = camera.read()
            if frame is None:
                camera.close()
                time.sleep(0.5)
                continue
            if now - conductor.last_seen > IDLE_AFTER:
                # Nobody's gesturing: look less often to save power.
                time.sleep(1 / IDLE_FPS)

            # VIDEO mode needs strictly increasing timestamps.
            ts = max(last_ts + 1, int((now - start) * 1000))
            last_ts = ts
            # Crop to the centre square: the tracker assumes a square, and
            # cropping (unlike padding) keeps the hand as big as possible.
            # A swipe that runs off the side still counts (EXIT_SWIPE).
            h, w = frame.shape[:2]
            side = min(w, h)
            y0, x0 = (h - side) // 2, (w - side) // 2
            frame = frame[y0:y0 + side, x0:x0 + side]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = recognizer.recognize_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)

            landmarks = result.hand_landmarks[0] if result.hand_landmarks else None
            world = result.hand_world_landmarks[0] if result.hand_world_landmarks else None
            pose = hand_pose(world, landmarks) if landmarks and world else None
            if args.debug:
                pc = palm_center(landmarks) if landmarks else None
                print(f"{now - start:7.2f} light={frame.mean():5.1f} hand={'yes' if pc else 'no '} "
                      f"pose={pose or '-':9} palm={f'{pc[0]:.2f},{pc[1]:.2f}' if pc else '-':10} "
                      f"state={conductor.state}", file=sys.stderr, flush=True)
            conductor.update(now, pose, landmarks)
    except KeyboardInterrupt:
        pass
    finally:
        camera.close()
        recognizer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
