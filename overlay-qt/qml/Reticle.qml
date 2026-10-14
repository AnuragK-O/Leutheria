import QtQuick

// The built-in placeholder for a state with no asset: the reticle.
// A port of overlay.css `.orb` (the frame), `.orb::before` (the core) and
// `.orb::after` (the ring) and their per-state rules; DESIGN.md "Reticle".
// Three flat parts in the state colour, sized from the slot side S:
//   frame  four corner brackets, arms 0.16 S, 1 px
//   core   a solid square 0.26 S, rotated 45 degrees
//   ring   a 1 px square outline 0.50 S, rotated 45 degrees, 45% at rest
//
//   state                  core                          ring
//   listening              opacity 1 -> .45 -> 1, 2.8 s  opacity/scale follow the level
//   hearing                scale follows the level       2 px, opacity/scale follow the level
//   transcribing           scale .7                      opacity 1, one turn per 1.1 s
//   thinking               one turn per 2.6 s            .45 (see the note on `idle` below)
//   speaking               still                         scale .55 -> 1.35, fading out, every 1.5 s
//   awaiting_confirmation  blinks 1 / .25 in two steps   opacity 1
//
// Animations only drive the phase properties; every visual property is a
// binding on (state, level, phase), so leaving a state can't leave an
// animation's last value stuck on screen. Nothing ticks while the window is
// hidden (BUGS.md #33): it stays in its last state between sessions.
Item {
    id: reticle

    required property string visualState
    required property real level // smoothed, 0..1
    required property Theme theme

    readonly property color tone: theme.tone(visualState)
    readonly property real side: Math.min(width, height)
    readonly property bool onScreen: Window.window?.visible ?? false

    readonly property bool listening: visualState === "listening"
    readonly property bool hearing: visualState === "hearing"
    readonly property bool transcribing: visualState === "transcribing"
    readonly property bool thinking: visualState === "thinking"
    readonly property bool speaking: visualState === "speaking"
    readonly property bool confirming: visualState === "awaiting_confirmation"

    // --- phases ---------------------------------------------------------------
    property real idle: 1      // @keyframes idle: opacity, 50% at .45
    property real turn: 0      // @keyframes turn: extra degrees, 0..360
    property real emit: 0      // @keyframes emit: progress 0..1
    property real blink: 1     // @keyframes blink under steps(2, jump-none)

    // CSS ease-in-out, per keyframe segment.
    readonly property var easeInOut: [0.42, 0, 0.58, 1, 1, 1]

    // `idle` animates opacity towards .45 at 50% and back to the element's
    // own value. On the listening core that's 1 -> .45 -> 1. On the thinking
    // ring the element's value is already .45, so in Electron the ring holds
    // at .45 -- matched here (DESIGN.md describes a pulse; see README).
    SequentialAnimation on idle {
        running: reticle.onScreen && reticle.listening
        loops: Animation.Infinite
        NumberAnimation { from: 1; to: 0.45; duration: 1400; easing.type: Easing.BezierSpline; easing.bezierCurve: reticle.easeInOut }
        NumberAnimation { from: 0.45; to: 1; duration: 1400; easing.type: Easing.BezierSpline; easing.bezierCurve: reticle.easeInOut }
    }
    NumberAnimation on turn {
        running: reticle.onScreen && (reticle.transcribing || reticle.thinking)
        from: 0; to: 360
        duration: reticle.transcribing ? 1100 : 2600
        loops: Animation.Infinite
    }
    NumberAnimation on emit {
        running: reticle.onScreen && reticle.speaking
        from: 0; to: 1; duration: 1500
        loops: Animation.Infinite
        easing.type: Easing.BezierSpline; easing.bezierCurve: reticle.theme.ease
    }
    // keyframes 0% 1, 50% .25, 100% 1 under steps(2, jump-none): each half
    // holds its start value for its first half and its end value for its
    // second, so 1 for 250 ms, .25 for 500 ms, 1 for 250 ms. Hard steps, no fade.
    SequentialAnimation on blink {
        running: reticle.onScreen && reticle.confirming
        loops: Animation.Infinite
        PropertyAction { target: reticle; property: "blink"; value: 1 }
        PauseAnimation { duration: 250 }
        PropertyAction { target: reticle; property: "blink"; value: 0.25 }
        PauseAnimation { duration: 500 }
        PropertyAction { target: reticle; property: "blink"; value: 1 }
        PauseAnimation { duration: 250 }
    }

    // --- the frame --------------------------------------------------------------
    CutBox {
        anchors.fill: parent
        lineWidth: 0
        brackets: CutBox.AllBrackets
        bracketArm: reticle.side * 0.16
        bracketThickness: reticle.theme.hair
        bracketColor: reticle.tone
    }

    // --- the core -----------------------------------------------------------------
    Rectangle {
        anchors.centerIn: parent
        width: reticle.side * 0.26
        height: width
        antialiasing: true
        color: reticle.tone
        rotation: 45 + (reticle.thinking ? reticle.turn : 0)
        scale: reticle.hearing ? 0.8 + reticle.level * 0.9
             : reticle.transcribing ? 0.7
             : 1
        opacity: reticle.listening ? reticle.idle
               : reticle.confirming ? reticle.blink
               : 1
        Behavior on color { ColorAnimation { duration: reticle.theme.tBase } }
    }

    // --- the ring -------------------------------------------------------------------
    Rectangle {
        anchors.centerIn: parent
        width: reticle.side * 0.5
        height: width
        antialiasing: true
        color: "transparent"
        border.width: reticle.hearing ? reticle.theme.rule : reticle.theme.hair
        border.color: reticle.tone
        rotation: 45 + (reticle.transcribing ? reticle.turn : 0)
        scale: reticle.listening ? 0.92 + reticle.level * 0.3
             : reticle.hearing ? 0.9 + reticle.level * 0.5
             : reticle.speaking ? 0.55 + reticle.emit * 0.8
             : 1
        opacity: reticle.listening ? 0.3 + reticle.level * 0.7
               : reticle.hearing ? 0.4 + reticle.level * 0.6
               : reticle.speaking ? 1 - reticle.emit
               : reticle.transcribing || reticle.confirming ? 1
               : 0.45
        Behavior on border.color { ColorAnimation { duration: reticle.theme.tBase } }
    }
}
