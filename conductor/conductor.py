#!/usr/bin/env python3
"""Conductor: drive Hyprland with hand gestures seen by the webcam.

Every frame goes through MediaPipe's gesture recognizer, locally; no image
leaves this process or touches the disk. Nothing happens until you arm it by
holding up an open palm. While armed, one gesture fires an action and
disarms again, so bringing your hand back after a swipe can't trigger the
opposite swipe:

  swipe left / right   previous / next workspace
  swipe up             toggle fullscreen
  swipe down           toggle the scratchpad
  thumb up (hold)      play / pause
  victory (hold)       next track

Directions are as you see them: the camera image is mirrored first.

Actions can be overridden in ~/.config/omarchy/conductor.json, e.g.
  {"actions": {"swipe-down": "", "victory": "omarchy-shell media previous"}}
A value starting with "hl." is a Hyprland dispatcher, anything else runs as a
shell command, and "" turns that gesture off.

The camera is released (and its light goes off) while the screen is locked.
Progress is reported to the Omarchy shell as JSON lines on stdout.
"""

import argparse
import json
import os
import subprocess
import sys
import time
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
    "swipe-down": 'hl.dsp.workspace.toggle_special("scratchpad")',
    "thumb-up": "omarchy-shell media playPause",
    "victory": "omarchy-shell media next",
}

# Recognizer labels for the hold gestures.
POSES = {"Thumb_Up": "thumb-up", "Victory": "victory"}

ARM_HOLD = 0.3        # seconds of open palm to arm
POSE_HOLD = 0.45      # seconds a hold gesture must be held
ARMED_TIMEOUT = 2.5   # disarm if nothing happens for this long
LOST_GRACE = 0.4      # tolerate the hand vanishing (motion blur) this long
MIN_SCORE = 0.6       # recognizer confidence for a pose to count
SWIPE_WINDOW = 0.45   # a swipe has to happen within this many seconds
SWIPE_X = 0.20        # ...and cover this share of the frame width
SWIPE_Y = 0.18        # ...or of its height
IDLE_AFTER = 20.0     # no hand for this long: drop to IDLE_FPS
IDLE_FPS = 5
LOCK_POLL = 1.0


def emit(**event):
    print(json.dumps(event), flush=True)


def load_actions(path):
    actions = dict(DEFAULT_ACTIONS)
    try:
        with open(path) as f:
            user = json.load(f).get("actions", {})
        actions.update({k: str(v) for k, v in user.items() if k in DEFAULT_ACTIONS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as e:
        print(f"conductor: ignoring {path}: {e}", file=sys.stderr)
    return actions


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


def palm_center(landmarks):
    # Wrist and the four knuckles: steadier than any fingertip. Mirrored, so
    # moving your hand to your right moves it right here too.
    pts = [landmarks[i] for i in (0, 5, 9, 13, 17)]
    return 1 - sum(p.x for p in pts) / 5, sum(p.y for p in pts) / 5


class Camera:
    def __init__(self, device):
        self.device = device
        self.cap = None

    def open(self):
        if self.cap is None:
            cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            if not cap.isOpened():
                cap.release()
                return False
            self.cap = cap
        return True

    def read(self):
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class Conductor:
    def __init__(self, actions, dry_run):
        self.actions = actions
        self.dry_run = dry_run
        self.state = None
        self.armed_at = 0.0
        self.palm_since = None
        self.pose = None
        self.pose_since = 0.0
        self.last_seen = 0.0
        self.track = deque()

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
        self.palm_since = None
        self.pose = None
        self.track.clear()

    def update(self, now, label, score, landmarks):
        if landmarks is None:
            if now - self.last_seen > LOST_GRACE:
                self.disarm()
                self.set_state("watching")
            return
        self.last_seen = now
        confident = label if score >= MIN_SCORE else None

        if not self.armed_at:
            if confident == "Open_Palm":
                self.palm_since = self.palm_since or now
                if now - self.palm_since >= ARM_HOLD:
                    self.armed_at = now
                    self.track.clear()
            else:
                self.palm_since = None
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
        _, x0, y0 = self.track[0]
        dx, dy = x - x0, y - y0
        if abs(dx) >= SWIPE_X and abs(dx) > 2 * abs(dy):
            return self.fire("swipe-right" if dx > 0 else "swipe-left")
        if abs(dy) >= SWIPE_Y and abs(dy) > 2 * abs(dx):
            return self.fire("swipe-down" if dy > 0 else "swipe-up")

        # Holds: the same pose for POSE_HOLD.
        pose = POSES.get(confident)
        if pose != self.pose:
            self.pose, self.pose_since = pose, now
        elif pose and now - self.pose_since >= POSE_HOLD:
            self.fire(pose)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
                        "omarchy-conductor")
    config = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                          "omarchy", "conductor.json")
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--device", type=int, default=0, help="video device number (default 0)")
    p.add_argument("--model", default=os.path.join(data, "gesture_recognizer.task"))
    p.add_argument("--config", default=config)
    p.add_argument("--dry-run", action="store_true", help="report gestures without acting")
    args = p.parse_args()

    if not os.path.exists(args.model):
        emit(event="error", message=f"model missing: run {os.path.join(here, '..', 'tools', 'setup.sh')}")
        return 1

    recognizer = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
        base_options=BaseOptions(model_asset_path=args.model),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    ))
    camera = Camera(args.device)
    conductor = Conductor(load_actions(args.config), args.dry_run)
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
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = recognizer.recognize_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts)

            if result.hand_landmarks:
                top = result.gestures[0][0] if result.gestures and result.gestures[0] else None
                conductor.update(now, top.category_name if top else None, top.score if top else 0.0,
                                 result.hand_landmarks[0])
            else:
                conductor.update(now, None, 0.0, None)
    except KeyboardInterrupt:
        pass
    finally:
        camera.close()
        recognizer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
