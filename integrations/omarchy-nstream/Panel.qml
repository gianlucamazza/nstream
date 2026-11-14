import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Ui as Ui
import qs.Commons
import qs.Commons as Commons

// nstream remote: the cast in progress (play/pause, seek, volume, subtitle shift,
// stop) and continue-watching, cast to the TV or played locally in mpv. Data and
// actions come from helper.py, which drives `nstream --json` and validates every
// argument; everything it returns is shown as plain text.
BarWidget {
  id: root
  moduleName: "gianlucamazza.nstream"

  property bool opened: false
  property string target: "cast"
  property bool targetChosen: false

  property var entries: []
  property var session: null
  property var status: null
  property string historyRaw: ""
  property string historyError: ""

  property string message: ""
  property bool messageIsError: false
  // Title being started; "" when no continue is running.
  property string starting: ""

  readonly property bool casting: session !== null
  readonly property bool playing: status !== null && status.state === "PLAYING"
  readonly property bool paused: status !== null && status.state === "PAUSED"
  readonly property real duration: status ? status.duration : 0
  readonly property int historyLimit: Math.max(1, Math.min(30, Number(setting("historyLimit", 8)) || 8))
  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property string helper: Qt.resolvedUrl("helper.py").toString().replace(/^file:\/\//, "")

  readonly property string iconMovie: "󰎁"
  readonly property string iconCast: "󰄙"
  readonly property string iconPlay: "󰐊"
  readonly property string iconPause: "󰏤"
  readonly property string iconStop: "󰓛"
  readonly property string iconBack: "󰑟"
  readonly property string iconForward: "󰈑"

  implicitWidth: iconButton.implicitWidth
  implicitHeight: barSize

  Commons.Translations { id: tr; directory: Qt.resolvedUrl("i18n") }

  function close() { opened = false }
  function helperCommand(args) { return ["python3", "-I", "-B", helper].concat(args) }

  function pollHistory() {
    if (historyProc.running) return
    historyProc.command = helperCommand(["history"])
    historyProc.running = true
  }
  function pollStatus() {
    if (statusProc.running || !casting) return
    statusProc.command = helperCommand(["status"])
    statusProc.running = true
  }
  function control(args) {
    if (controlProc.running) return
    message = ""
    controlProc.command = helperCommand(args)
    controlProc.running = true
  }
  function resumeEntry(entry) {
    if (continueProc.running) return
    starting = entry.title
    message = ""
    continueProc.command = helperCommand(["continue", target, entry.title])
    continueProc.running = true
  }

  function notify(summary, body) {
    Quickshell.execDetached(["notify-send", "-a", "nstream", "-i", "video-x-generic", summary, body])
  }

  function receive(text) {
    var d
    try { d = JSON.parse(text) } catch (e) { return }
    if (!d || typeof d !== "object") return
    if (d.kind === "history") {
      if (!d.ok) { historyError = String(d.error || ""); return }
      historyError = ""
      if (!targetChosen) target = d.preferCast ? "cast" : "local"
      if (text === historyRaw) return
      historyRaw = text
      entries = d.entries instanceof Array ? d.entries : []
      var wasCasting = casting
      session = d.session && typeof d.session === "object" ? d.session : null
      if (!session) status = null
      else if (!wasCasting) pollStatus()
    } else if (d.kind === "status") {
      status = d.ok ? d : null
    } else if (d.kind === "continue") {
      var title = starting
      starting = ""
      if (!d.ok) {
        messageIsError = true
        message = String(d.error || "")
        notify(tr.t("notify.failed").arg(title), message)
        return
      }
      messageIsError = false
      message = d.notice ? String(d.notice) : ""
      if (d.local) notify(tr.t("notify.local").arg(title), "")
      else notify(tr.t("notify.cast").arg(String(d.title || title)), String(d.device || ""))
      pollHistory()
    } else if (!d.ok) {
      messageIsError = true
      message = String(d.error || "")
    } else if (d.kind === "stop") {
      session = null
      status = null
      pollHistory()
    }
  }

  function clock(sec) {
    sec = Math.max(0, Math.floor(Number(sec) || 0))
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60
    var mm = (h > 0 && m < 10 ? "0" : "") + m, ss = (s < 10 ? "0" : "") + s
    return (h > 0 ? h + ":" : "") + mm + ":" + ss
  }
  function episodeLabel(e) {
    if (e.type !== "series" || e.season <= 0) return ""
    return "S" + (e.season < 10 ? "0" : "") + e.season + "E" + (e.episode < 10 ? "0" : "") + e.episode
  }
  function entryCaption(e) {
    var parts = []
    var ep = episodeLabel(e)
    if (ep !== "") parts.push(ep)
    if (e.next) parts.push(tr.t("entry.next"))
    else if (e.duration > 0) {
      var pct = Math.round(100 * e.position / e.duration)
      parts.push(tr.t("entry.progress").arg(Commons.I18n.formatNumber(pct, 0)).arg(clock(e.duration - e.position)))
    }
    return parts.join(" · ")
  }
  function heroMeta() {
    if (starting !== "") return tr.t("starting").arg(starting)
    if (!casting) return tr.t("idle")
    var device = session.device || (status ? status.device : "")
    return device !== "" ? tr.t("casting.device").arg(device) : tr.t("casting")
  }
  function stateLabel() {
    if (!status) return tr.t("state.unknown")
    if (playing) return tr.t("state.playing")
    if (paused) return tr.t("state.paused")
    if (status.state === "BUFFERING") return tr.t("state.buffering")
    if (status.state === "IDLE" || status.state === "") return tr.t("state.idle")
    return status.state
  }

  onOpenedChanged: { message = ""; if (opened) { pollHistory(); pollStatus() } }

  Commons.ShellIpc {
    target: "gianlucamazza.nstream"
    function toggle(): void { root.opened = !root.opened }
    function open(): void { root.opened = true }
    function close(): void { root.opened = false }
  }

  Process { id: historyProc; stdout: StdioCollector { onStreamFinished: root.receive(text) } }
  Process { id: statusProc; stdout: StdioCollector { onStreamFinished: root.receive(text) } }
  Process {
    id: controlProc
    stdout: StdioCollector { onStreamFinished: { root.receive(text); root.pollStatus() } }
  }
  Process { id: continueProc; stdout: StdioCollector { onStreamFinished: root.receive(text) } }

  // History is a local read (no network): cheap enough to poll in the background.
  Timer {
    interval: root.opened ? 5000 : 30000
    running: true; repeat: true; triggeredOnStart: true
    onTriggered: root.pollHistory()
  }
  // The receiver is only asked while the panel is open and a cast is live.
  Timer {
    interval: 3000
    running: root.opened && root.casting
    repeat: true
    onTriggered: root.pollStatus()
  }

  component Plain: Text {
    textFormat: Text.PlainText
    font.family: Style.font.family
    font.pixelSize: Style.font.body
    color: Commons.Color.foreground
    elide: Text.ElideRight
  }
  component Caption: Plain { font.pixelSize: Style.font.caption; opacity: 0.6 }

  BarIconButton {
    id: iconButton
    bar: root.bar
    anchors.left: parent.left
    anchors.verticalCenter: parent.verticalCenter
    text: root.casting ? root.iconCast : root.iconMovie
    dimmed: !root.casting && root.starting === ""
    tooltipText: root.casting
      ? tr.t("tooltip.casting").arg(root.session.title || "?")
      : root.starting !== "" ? tr.t("starting").arg(root.starting) : tr.t("title")
    onPressed: function(button) { if (button === Qt.LeftButton) root.opened = !root.opened }
  }

  KeyboardPanel {
    id: panel
    anchorItem: iconButton; owner: root; bar: root.bar; open: root.opened
    contentWidth: fittedContentWidth(Style.space(420))
    contentHeight: fittedContentHeight(Math.min(Style.space(600), body.implicitHeight))
    focusTarget: viewport
    Flickable {
      id: viewport
      anchors.fill: parent; clip: true
      contentWidth: width; contentHeight: body.implicitHeight
      boundsBehavior: Flickable.StopAtBounds
      Keys.onEscapePressed: root.close()
      Column {
        id: body
        width: viewport.width
        spacing: Style.space(10)

        Ui.PanelHero {
          width: parent.width
          title: tr.t("title")
          meta: root.heroMeta()
          trailingControl: Component {
            Ui.Button {
              text: tr.t("open")
              onClicked: { Quickshell.execDetached(["nstream-fuzzel"]); root.close() }
            }
          }
        }
        Plain {
          width: parent.width
          visible: root.message !== ""
          text: root.message
          wrapMode: Text.Wrap; elide: Text.ElideNone
          maximumLineCount: 6
          font.pixelSize: Style.font.bodySmall
          color: root.messageIsError ? root.urgent : Commons.Color.foreground
        }

        // Now playing on the TV
        Column {
          width: parent.width
          visible: root.casting
          spacing: Style.space(6)
          Plain { width: parent.width; text: root.session ? root.session.title : ""; elide: Text.ElideMiddle }
          Caption {
            width: parent.width
            text: root.stateLabel() + (root.duration > 0
              ? " · " + root.clock(root.status.position) + " / " + root.clock(root.duration) : "")
          }
          Ui.PanelSlider {
            id: seekSlider
            width: parent.width
            bar: root.bar
            visible: root.duration > 0
            minimum: 0
            maximum: Math.max(1, root.duration)
            step: 1
            value: root.status ? root.status.position : 0
            onReleased: function(value) { root.control(["seekto", String(Math.round(value))]) }
          }
          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.space(6)
            Ui.PanelActionButton {
              iconText: root.iconBack; tooltipText: tr.t("act.back")
              enabled: !controlProc.running
              onClicked: root.control(["seek", "-30"])
            }
            Ui.PanelActionButton {
              iconText: root.playing ? root.iconPause : root.iconPlay
              tooltipText: root.playing ? tr.t("act.pause") : tr.t("act.play")
              enabled: !controlProc.running
              onClicked: root.control([root.playing ? "pause" : "resume"])
            }
            Ui.PanelActionButton {
              iconText: root.iconForward; tooltipText: tr.t("act.forward")
              enabled: !controlProc.running
              onClicked: root.control(["seek", "30"])
            }
            Ui.PanelActionButton {
              iconText: root.iconStop; tooltipText: tr.t("act.stop")
              hoverColor: root.urgent
              enabled: !controlProc.running
              onClicked: root.control(["stop"])
            }
          }
          Row {
            width: parent.width
            spacing: Style.space(8)
            visible: root.status !== null && root.status.volume >= 0
            Caption { id: volumeLabel; anchors.verticalCenter: parent.verticalCenter; text: tr.t("volume") }
            Ui.PanelSlider {
              width: parent.width - volumeLabel.width - parent.spacing
              anchors.verticalCenter: parent.verticalCenter
              bar: root.bar
              minimum: 0; maximum: 100; step: 1; integer: true
              value: root.status ? root.status.volume : 0
              onReleased: function(value) { root.control(["volume", String(Math.round(value))]) }
            }
          }
          Row {
            spacing: Style.space(4)
            Caption { anchors.verticalCenter: parent.verticalCenter; text: tr.t("subtitles") }
            Ui.Button {
              text: tr.t("sub.earlier"); bordered: true; enabled: !controlProc.running
              onClicked: root.control(["subshift", "-0.5"])
            }
            Ui.Button {
              text: tr.t("sub.later"); bordered: true; enabled: !controlProc.running
              onClicked: root.control(["subshift", "0.5"])
            }
          }
          Ui.PanelSeparator { width: parent.width }
        }

        // Continue watching
        Ui.PanelSectionHeader { width: parent.width; text: tr.t("continue") }
        Ui.ButtonGroup {
          focusable: false
          value: root.target
          options: [
            { value: "cast", label: tr.t("target.cast") },
            { value: "local", label: tr.t("target.local") }
          ]
          onChanged: function(value) { root.target = value; root.targetChosen = true }
        }
        Column {
          width: parent.width
          spacing: Style.space(2)
          Caption {
            width: parent.width
            visible: root.historyError !== "" || root.entries.length === 0
            text: root.historyError !== "" ? root.historyError : tr.t("empty")
            wrapMode: Text.Wrap; elide: Text.ElideNone; maximumLineCount: 4
          }
          Repeater {
            model: root.entries.slice(0, root.historyLimit)
            Item {
              id: entryRow
              required property var modelData
              width: parent ? parent.width : 0
              implicitHeight: Math.max(entryText.implicitHeight, playButton.implicitHeight) + Style.space(8)
              opacity: root.starting !== "" && root.starting !== modelData.title ? 0.45 : 1
              Column {
                id: entryText
                anchors.left: parent.left
                anchors.right: playButton.left
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)
                Plain { width: parent.width; text: entryRow.modelData.title }
                Caption { width: parent.width; text: root.entryCaption(entryRow.modelData); visible: text !== "" }
              }
              Ui.PanelActionButton {
                id: playButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                iconText: root.iconPlay
                tooltipText: root.target === "cast" ? tr.t("act.castHere") : tr.t("act.playHere")
                enabled: !continueProc.running
                onClicked: root.resumeEntry(entryRow.modelData)
              }
            }
          }
        }
      }
    }
  }
}
