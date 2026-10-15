# Conductor

Control Hyprland with your hands: hold up an open palm to your webcam, then
swipe or make a sign. Hand tracking runs locally with Google's MediaPipe; no
image leaves your computer or is written to disk.

## Gestures

Every gesture starts with **an open palm held up for a moment**. That arms
Conductor (the bar icon lights up), so typing, scratching your head or
waving at someone does nothing. One gesture then fires and disarms it again,
so bringing your hand back after a swipe can't trigger the opposite swipe.

| Gesture | Action |
|---|---|
| Swipe left / right | Previous / next workspace |
| Swipe up | Full screen |
| Swipe down | Scratchpad |
| Thumb up (hold) | Play / pause |
| Victory sign (hold) | Next track |

Directions are as you see them. Arming times out after 2.5 seconds.

To change what a gesture does, create `~/.config/omarchy/conductor.json`:

```json
{
  "actions": {
    "swipe-down": "",
    "victory": "omarchy-shell media previous",
    "swipe-up": "hl.dsp.window.fullscreen({ mode = \"maximized\" })"
  }
}
```

A value starting with `hl.` is a Hyprland dispatcher, anything else runs as
a shell command, and `""` turns that gesture off. Switch Conductor off and on
to apply changes.

## Install

```bash
omarchy plugin add https://github.com/TylerSw1ft/omarchy-conductor.git --enable
```

Then open the hand icon in the bar and click **Set up**. That runs
`tools/setup.sh`, which installs MediaPipe into its own Python environment
and downloads Google's gesture model (checked against a pinned SHA-256),
about 500 MB in `~/.local/share/omarchy-conductor/`. You need `python3` and
`curl`, and a webcam at `/dev/video0`.

Conductor starts **off**. Switch it on in the panel, or right-click the icon.

## Privacy and power

- The camera is only open while Conductor is switched on, and the bar icon
  shows it. It's released while the screen is locked.
- Frames are processed in memory and dropped; nothing is saved or sent.
- With no hand in view it looks 5 times a second, about 8% of one CPU core
  on a recent laptop, and speeds up when it sees a hand.

## How it works

`Service.qml` runs `conductor/conductor.py` while Conductor is on. The
script reads the webcam with OpenCV, runs MediaPipe's gesture recognizer on
each frame, and tracks the palm to spot swipes. It runs actions with
`hyprctl dispatch` or a shell command, and reports its state and each
gesture to the shell as JSON lines. You can run it by hand to test, without
it doing anything:

```bash
~/.local/share/omarchy-conductor/venv/bin/python conductor/conductor.py --dry-run
```

## Remove

```bash
omarchy plugin remove tylersw1ft.conductor
rm -r ~/.local/share/omarchy-conductor ~/.local/state/omarchy/conductor.json ~/.config/omarchy/conductor.json
```
