import QtQuick
import QtQuick.Effects
import QtQuick.Shapes

// The built-in placeholder: one orb, restyled per state, driven by the mic
// level. A port of overlay.css `.orb`, `.orb::before` (ring) and `.orb::after`
// (second ring) and their per-state rules:
//   listening             breathing, ring follows the level
//   hearing               second ring appears and tracks the voice harder
//   transcribing          a spinning arc replaces the ring
//   thinking              the orb's light rotates (conic gradient)
//   speaking              two rings ripple outward
//   awaiting_confirmation amber, faster breathing
//
// Animations only drive the "phase" properties below; every visual property
// is a plain binding on (state, level, phase), so leaving a state can't leave
// an animation's last value stuck on screen.
Item {
    id: orb

    required property string visualState
    required property real level
    required property Theme theme

    readonly property bool amber: visualState === "awaiting_confirmation"
    readonly property color core: amber ? theme.amberCore : theme.orbCore
    readonly property color mid: amber ? theme.warning : theme.orbMid
    readonly property color edge: amber ? theme.amberEdge : theme.orbEdge
    readonly property color ring: amber ? theme.amberRing : theme.ring
    readonly property real d: Math.min(width, height) * 0.64 // .orb { width: 64% }
    // The window is hidden between sessions, still in its last state; nothing
    // should tick while there's nothing on screen.
    readonly property bool onScreen: Window.window?.visible ?? false

    // --- phases, 0..1 (or degrees) -------------------------------------------
    property real breathe: 0   // brightness pulse
    property real bodySpin: 0  // thinking
    property real arcSpin: 0   // transcribing
    property real ripple1: 0   // speaking
    property real ripple2: 0

    SequentialAnimation on breathe {
        running: orb.onScreen && (orb.visualState === "listening" || orb.amber)
        loops: Animation.Infinite
        NumberAnimation { to: 1; duration: orb.amber ? 900 : 1600; easing.type: Easing.InOutSine }
        NumberAnimation { to: 0; duration: orb.amber ? 900 : 1600; easing.type: Easing.InOutSine }
    }
    NumberAnimation on bodySpin {
        running: orb.onScreen && orb.visualState === "thinking"
        from: 0; to: 360; duration: 2400
        loops: Animation.Infinite
    }
    NumberAnimation on arcSpin {
        running: orb.onScreen && orb.visualState === "transcribing"
        from: 0; to: 360; duration: 900
        loops: Animation.Infinite
    }
    NumberAnimation on ripple1 {
        running: orb.onScreen && orb.visualState === "speaking"
        from: 0; to: 1; duration: 1600
        loops: Animation.Infinite
        easing.type: Easing.BezierSpline; easing.bezierCurve: orb.theme.ease
    }
    SequentialAnimation on ripple2 {
        running: orb.onScreen && orb.visualState === "speaking"
        // animation-delay: 0.8s, invisible until then (phase 1 = faded out)
        PropertyAction { target: orb; property: "ripple2"; value: 1 }
        PauseAnimation { duration: 800 }
        NumberAnimation {
            from: 0; to: 1; duration: 1600
            loops: Animation.Infinite
            easing.type: Easing.BezierSpline; easing.bezierCurve: orb.theme.ease
        }
    }

    // --- the orb and its rings, all centred ----------------------------------
    Item {
        id: body
        width: orb.d
        height: orb.d
        anchors.centerIn: parent

        // `transform` on .orb; thinking's spin animation replaces the scale
        // entirely in CSS, so it does here too.
        scale: orb.visualState === "thinking" ? 1
             : orb.visualState === "hearing" ? 0.96 + orb.level * 0.2
             : 0.92 + orb.level * 0.22
        rotation: orb.visualState === "thinking" ? orb.bodySpin : 0

        // box-shadow: 0 0 (6px + level*22px) mid@70%
        RectangularShadow {
            anchors.fill: parent
            radius: width / 2
            blur: 6 + orb.level * 22
            color: Qt.rgba(orb.mid.r, orb.mid.g, orb.mid.b, 0.7)
        }

        Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeWidth: -1
                // radial-gradient(circle at 35% 30%, core, mid 55%, edge), or for
                // thinking a conic gradient from the top, clockwise. Qt's conical
                // gradient runs counter-clockwise, hence the reversed stops.
                fillGradient: orb.visualState === "thinking" ? conic : radial
                PathAngleArc {
                    centerX: orb.d / 2; centerY: orb.d / 2
                    radiusX: orb.d / 2; radiusY: orb.d / 2
                    startAngle: 0; sweepAngle: 360
                }
            }
        }
        RadialGradient {
            id: radial
            centerX: orb.d * 0.35; centerY: orb.d * 0.3
            focalX: centerX; focalY: centerY
            centerRadius: orb.d * Math.hypot(0.65, 0.7) // to the farthest corner
            GradientStop { position: 0; color: orb.core }
            GradientStop { position: 0.55; color: orb.mid }
            GradientStop { position: 1; color: orb.edge }
        }
        ConicalGradient {
            id: conic
            centerX: orb.d / 2; centerY: orb.d / 2
            angle: 90
            GradientStop { position: 0; color: orb.edge }
            GradientStop { position: 1 / 3; color: orb.mid }
            GradientStop { position: 2 / 3; color: orb.core }
            GradientStop { position: 1; color: orb.edge }
        }

        // `@keyframes breathe { 50% { filter: brightness(1.18) } }`, as a sheen.
        Rectangle {
            anchors.fill: parent
            radius: width / 2
            color: "white"
            opacity: orb.visualState === "listening" || orb.amber ? orb.breathe * 0.14 : 0
        }

        // .orb::before -- the ring (inset: -6px)
        Rectangle {
            anchors.centerIn: parent
            width: orb.d + 12
            height: width
            radius: width / 2
            color: "transparent"
            border.width: 1.5
            border.color: orb.ring
            visible: orb.visualState !== "transcribing"
            scale: orb.visualState === "speaking" ? 0.9 + orb.ripple1 * 0.4 : 0.96 + orb.level * 0.14
            opacity: orb.visualState === "speaking" ? 0.8 * (1 - orb.ripple1)
                   : orb.visualState === "thinking" ? 0.35
                   : 0.25 + orb.level * 0.75
        }

        // .orb::after -- the second ring: hearing (inset -10px) and speaking (-6px)
        Rectangle {
            anchors.centerIn: parent
            width: orb.visualState === "hearing" ? orb.d + 20 : orb.d + 12
            height: width
            radius: width / 2
            color: "transparent"
            border.width: 1.5
            border.color: orb.ring
            visible: orb.visualState === "hearing" || orb.visualState === "speaking"
            scale: orb.visualState === "speaking" ? 0.9 + orb.ripple2 * 0.4 : 0.92 + orb.level * 0.1
            opacity: orb.visualState === "speaking" ? 0.8 * (1 - orb.ripple2) : orb.level * 0.9
        }

        // transcribing: the ring becomes a spinning arc (border-top + border-right)
        Shape {
            anchors.centerIn: parent
            width: orb.d + 12
            height: width
            visible: orb.visualState === "transcribing"
            rotation: orb.arcSpin
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                fillColor: "transparent"
                strokeColor: orb.theme.accent
                strokeWidth: 1.5
                capStyle: ShapePath.FlatCap
                PathAngleArc {
                    centerX: (orb.d + 12) / 2; centerY: centerX
                    radiusX: (orb.d + 12 - 1.5) / 2; radiusY: radiusX
                    startAngle: -135; sweepAngle: 180
                }
            }
        }
    }
}
