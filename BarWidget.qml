import QtQuick
import Quickshell
import qs.Commons
import qs.Ui

// Bar icon plus panel. The icon shows when the camera is on and lights up
// while a gesture is armed; after each gesture it briefly shows what fired.
// Left click opens the panel, right click switches gestures on or off.
Panel {
  id: root
  moduleName: "tylersw1ft.conductor"
  ipcTarget: "tylersw1ft.conductor"

  readonly property var service: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("tylersw1ft.conductor") : null
  readonly property bool on: service ? service.enabled : false
  readonly property bool installed: service ? service.installed : false
  readonly property string trackerState: service ? service.state : ""
  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property string font: bar ? bar.fontFamily : Style.font.family

  property string flashIcon: ""

  readonly property string statusText: {
    if (!service) return "Conductor service is not running"
    if (!installed) return service.setupRunning ? "Setting up…" : "Needs setup"
    if (!on) return "Off · camera not in use"
    switch (trackerState) {
    case "starting": return "Starting the camera…"
    case "watching": return "Camera on · hold up an open palm"
    case "hand": return "Hand seen · hold your palm open to arm"
    case "armed": return "Armed · swipe or make a sign"
    case "paused": return "Paused while the screen is locked"
    case "error": return service.error || "Camera error"
    default: return "Camera on"
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Connections {
    target: root.service
    function onGestured(gesture) {
      root.flashIcon = root.service.iconFor(gesture)
      flashTimer.restart()
    }
  }

  Timer {
    id: flashTimer
    interval: 900
    onTriggered: root.flashIcon = ""
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.flashIcon || (root.on ? "󰹇" : "󱠱")
    dimmed: !root.on || root.trackerState === "paused"
    active: root.opened || root.trackerState === "armed" || root.flashIcon !== ""
    useActiveColor: root.trackerState === "armed" || root.flashIcon !== ""
    tooltipText: root.opened ? "" : "Conductor · " + root.statusText
    onPressed: function(b) {
      if (b === Qt.RightButton) { if (root.service && root.installed) root.service.toggle() }
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onActivateRequested: if (root.service && root.installed) root.service.toggle()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(14)

        Text {
          visible: !root.service
          width: parent.width
          wrapMode: Text.WordWrap
          textFormat: Text.PlainText
          text: "The Conductor service isn't running. Restart the shell with `omarchy restart shell`."
          color: root.fg
          font.family: root.font
          font.pixelSize: Style.font.bodySmall
        }

        // ---------- Setup ----------
        Column {
          visible: !!root.service && !root.installed
          width: parent.width
          spacing: Style.space(10)

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: "Conductor needs Google's MediaPipe hand tracker and its gesture model, "
              + "about 500 MB in ~/.local/share/omarchy-conductor. Everything runs on this "
              + "computer; no images leave it."
            color: root.fg
            font.family: root.font
            font.pixelSize: Style.font.bodySmall
          }

          Button {
            width: parent.width
            text: root.service && root.service.setupRunning ? "Setting up…" : "Set up"
            iconText: "󰇚"
            enabled: !(root.service && root.service.setupRunning)
            fontSize: Style.font.bodySmall
            foreground: root.fg
            fontFamily: root.font
            horizontalPadding: Style.spacing.controlPaddingX
            verticalPadding: Style.spacing.controlPaddingY
            bordered: true
            onClicked: root.service.setup()
          }

          Text {
            visible: !!root.service && root.service.setupMessage !== ""
            width: parent.width
            wrapMode: Text.WordWrap
            textFormat: Text.PlainText
            text: root.service ? root.service.setupMessage : ""
            color: root.fg
            opacity: 0.6
            font.family: root.font
            font.pixelSize: Style.font.caption
          }
        }

        // ---------- On / off ----------
        Toggle {
          visible: root.installed
          width: parent.width
          label: "Hand gestures"
          description: root.statusText
          checked: root.on
          foreground: root.fg
          fontFamily: root.font
          onClicked: root.service.toggle()
        }

        Toggle {
          visible: root.installed
          width: parent.width
          label: "Infrared camera"
          description: root.service && root.service.camera === "infrared"
            ? "Works in the dark · fewer frames"
            : "Using the colour camera · needs a lit room"
          checked: root.service ? root.service.camera === "infrared" : false
          foreground: root.fg
          fontFamily: root.font
          onClicked: root.service.setCamera(root.service.camera === "infrared" ? "color" : "infrared")
        }

        PanelSeparator { visible: root.installed; foreground: root.fg }

        // ---------- Gesture list ----------
        Column {
          width: parent.width
          spacing: Style.space(8)
          opacity: root.on ? 1.0 : 0.5

          PanelSectionHeader {
            text: "HOLD UP AN OPEN PALM, THEN"
            foreground: root.fg
            fontFamily: root.font
          }

          Repeater {
            model: root.service ? root.service.gestures : []
            Row {
              required property var modelData
              width: column.width
              spacing: Style.space(10)

              readonly property bool lit: root.flashIcon !== "" && root.service.lastGesture === modelData.id

              Text {
                width: Style.space(20)
                textFormat: Text.PlainText
                text: modelData.icon
                color: parent.lit ? Color.accent : root.fg
                font.family: root.font
                font.pixelSize: Style.font.body
              }
              Text {
                width: (column.width - Style.space(40)) / 2
                textFormat: Text.PlainText
                text: modelData.label
                color: parent.lit ? Color.accent : root.fg
                font.family: root.font
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                width: (column.width - Style.space(40)) / 2
                horizontalAlignment: Text.AlignRight
                textFormat: Text.PlainText
                text: modelData.action
                color: root.fg
                opacity: 0.7
                font.family: root.font
                font.pixelSize: Style.font.bodySmall
              }
            }
          }
        }

        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          textFormat: Text.PlainText
          text: "Signs are held for half a second. Right-click the icon to switch on or off · "
            + "change actions in ~/.config/omarchy/conductor.json"
          color: root.fg
          opacity: 0.5
          font.family: root.font
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
