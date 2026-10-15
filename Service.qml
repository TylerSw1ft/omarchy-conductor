import QtQuick
import Quickshell
import Quickshell.Io

// Runs conductor/conductor.py, the webcam hand tracker, while gestures are
// switched on, and stops it (releasing the camera) when they're switched off.
// The tracker reports its state and each gesture as JSON lines on stdout.
Item {
  id: root
  width: 0
  height: 0
  visible: false

  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  readonly property string pluginDir: decodeURIComponent(Qt.resolvedUrl(".").toString().replace(/^file:\/\//, "")).replace(/\/$/, "")
  readonly property string dataDir: (Quickshell.env("XDG_DATA_HOME") || Quickshell.env("HOME") + "/.local/share") + "/omarchy-conductor"
  readonly property string python: dataDir + "/venv/bin/python"

  readonly property bool enabled: prefs.enabled
  // "color" or "infrared": which camera the tracker reads.
  readonly property string camera: prefs.camera === "infrared" ? "infrared" : "color"
  readonly property bool wanted: enabled && installed
  // installed: the tracker's environment and model exist (checked at start
  // and after setup). setupRunning: tools/setup.sh is running.
  property bool installed: false
  property bool setupRunning: false
  property string setupMessage: ""
  // Tracker state: "starting", "watching" (no hand), "hand", "armed",
  // "paused" (screen locked) or "error"; "" while it isn't running.
  property string state: ""
  property string error: ""
  property string lastGesture: ""

  // Gestures in display order, with what they do by default.
  readonly property var gestures: [
    { id: "swipe-left", label: "Sweep left", action: "Previous workspace", icon: "󰁍" },
    { id: "swipe-right", label: "Sweep right", action: "Next workspace", icon: "󰁔" },
    { id: "swipe-up", label: "Palm, swipe up", action: "Full screen", icon: "󰁝" },
    { id: "thumb-up", label: "Palm, thumb up", action: "Play / pause", icon: "󰔓" },
    { id: "victory", label: "Palm, victory", action: "Next track", icon: "󰒭" }
  ]

  signal gestured(string gesture)

  function setEnabled(value) {
    prefs.enabled = !!value
  }

  function toggle() {
    setEnabled(!prefs.enabled)
  }

  function setCamera(value) {
    prefs.camera = value === "infrared" ? "infrared" : "color"
  }

  // Switching cameras restarts the tracker on the new one.
  onCameraChanged: if (tracker.running) { tracker.running = false; restartTimer.restart() }

  // Start or stop the tracker to match `wanted`. Done by hand rather than
  // by binding `running`, since a process that exits resets it.
  function sync() {
    if (wanted && !tracker.running) tracker.running = true
    else if (!wanted && tracker.running) tracker.running = false
  }

  onWantedChanged: sync()

  function iconFor(gesture) {
    for (var i = 0; i < gestures.length; i++)
      if (gestures[i].id === gesture) return gestures[i].icon
    return "󰹇"
  }

  function checkInstalled() {
    if (!installCheck.running) installCheck.running = true
  }

  function setup() {
    if (setupRunning) return
    setupMessage = "Starting…"
    setupRunning = true
    setupProcess.running = true
  }

  function handle(line) {
    var msg
    try { msg = JSON.parse(line) } catch (e) { return }
    if (msg.event === "state") {
      root.state = msg.state
      root.error = ""
    } else if (msg.event === "gesture") {
      root.lastGesture = msg.gesture
      root.gestured(msg.gesture)
    } else if (msg.event === "error") {
      root.state = "error"
      root.error = String(msg.message || "")
    }
  }

  Process {
    id: installCheck
    command: ["test", "-x", root.python, "-a", "-f", root.dataDir + "/gesture_recognizer.task"]
    onExited: function(exitCode) { root.installed = exitCode === 0 }
  }

  Process {
    id: setupProcess
    command: ["bash", root.pluginDir + "/tools/setup.sh"]
    stdout: SplitParser { onRead: function(data) { root.setupMessage = data } }
    stderr: SplitParser { onRead: function(data) { root.setupMessage = data } }
    onExited: function(exitCode) {
      root.setupRunning = false
      if (exitCode !== 0 && root.setupMessage === "") root.setupMessage = "Setup failed"
      root.checkInstalled()
    }
  }

  Process {
    id: tracker
    command: [root.python, root.pluginDir + "/conductor/conductor.py", "--camera", root.camera]
    stdout: SplitParser { onRead: function(data) { root.handle(data) } }
    onRunningChanged: if (running) { root.state = "starting"; root.error = "" }
    onExited: function(exitCode) {
      if (root.state !== "error") root.state = ""
      // Crashed while still wanted: try again shortly.
      if (root.wanted) restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 3000
    onTriggered: root.sync()
  }

  FileView {
    id: store
    path: (Quickshell.env("XDG_STATE_HOME") || Quickshell.env("HOME") + "/.local/state") + "/omarchy/conductor.json"
    watchChanges: true
    onFileChanged: reload()
    onAdapterUpdated: writeAdapter()
    onLoadFailed: function(error) {
      if (error === FileViewError.FileNotFound) writeAdapter()
    }

    JsonAdapter {
      id: prefs
      // Off until you switch it on: it uses the camera.
      property bool enabled: false
      property string camera: "color"
    }
  }

  Component.onCompleted: checkInstalled()
}
