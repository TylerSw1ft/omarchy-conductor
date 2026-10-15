#!/bin/bash
# Install Conductor's hand tracker: a private Python environment with
# MediaPipe, and Google's gesture recognizer model. Everything goes into
# ~/.local/share/omarchy-conductor/ (about 500 MB); delete that folder to undo.

set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}/omarchy-conductor"
VENV="$DATA/venv"
MODEL="$DATA/gesture_recognizer.task"
MODEL_URL="https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task"
MODEL_SHA256="97952348cf6a6a4915c2ea1496b4b37ebabc50cbbf80571435643c455f2b0482"
MEDIAPIPE="mediapipe==1.1.1"

mkdir -p "$DATA"

if [[ ! -x $VENV/bin/python ]]; then
  echo "Creating Python environment in $VENV"
  python3 -m venv "$VENV"
fi
echo "Installing $MEDIAPIPE (this can take a minute)"
"$VENV/bin/pip" install --quiet --disable-pip-version-check "$MEDIAPIPE"

if ! echo "$MODEL_SHA256  $MODEL" | sha256sum --check --status 2>/dev/null; then
  echo "Downloading the gesture model"
  tmp="$MODEL.part"
  curl -fsSL -o "$tmp" "$MODEL_URL"
  if ! echo "$MODEL_SHA256  $tmp" | sha256sum --check --status; then
    rm -f "$tmp"
    echo "conductor setup: the downloaded model doesn't match its checksum" >&2
    exit 1
  fi
  mv "$tmp" "$MODEL"
fi

"$VENV/bin/python" -c "import mediapipe, cv2" 2>/dev/null
echo "Conductor is ready."
