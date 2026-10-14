import QtQuick
import Leutheria.Core

// styles.css `.btn.btn-sm` with `.btn-danger` (Decline) or `.btn-hot`
// (Approve): 28 px tall, Chakra Petch 11 px / 600 / 0.06em upper, the
// bottom-right corner cut 6 px, outline and fill swapped on hover and press
// over --t-fast. Hand-rolled rather than a QtQuick.Controls Button: the look
// is fixed by the Electron app's CSS.
Item {
    id: button

    enum Kind { Danger, Hot }

    required property string text
    required property Theme theme
    property int kind: OverlayButton.Danger
    signal clicked()

    readonly property bool hot: kind === OverlayButton.Hot
    readonly property bool hovered: mouse.containsMouse
    readonly property bool pressed: mouse.pressed

    // padding: 0 12px 0 10px
    implicitWidth: label.implicitWidth + 22
    implicitHeight: theme.hit

    CutBox {
        id: shape
        anchors.fill: parent
        corner: CutBox.BottomRight
        cut: button.theme.cutSm
        // .btn-hot: amber, mixed 82% with --text on hover and press.
        // .btn-danger: red 55% into --line on --surface; red on hover,
        // tinted 14% (24% pressed).
        readonly property color hotFill: button.hovered || button.pressed
                                         ? button.theme.mix(button.theme.amber, button.theme.text, 0.82)
                                         : button.theme.amber
        color: button.hot ? hotFill
             : button.pressed ? button.theme.mix(button.theme.red, button.theme.surface, 0.24)
             : button.hovered ? button.theme.mix(button.theme.red, button.theme.surface, 0.14)
             : button.theme.surface
        lineColor: button.hot ? hotFill
                 : button.hovered || button.pressed ? button.theme.red
                 : button.theme.mix(button.theme.red, button.theme.line, 0.55)
        Behavior on color { ColorAnimation { duration: button.theme.tFast } }
        Behavior on lineColor { ColorAnimation { duration: button.theme.tFast } }
    }

    Text {
        id: label
        x: 10
        anchors.verticalCenter: parent.verticalCenter
        text: button.text.toUpperCase()
        color: button.hot ? button.theme.textOnAccent : button.theme.red
        font: Typeface.ui(600, button.theme.fsXs, button.theme.trackButton)
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        onClicked: button.clicked()
    }
}
