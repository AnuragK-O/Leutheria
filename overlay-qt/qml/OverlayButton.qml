import QtQuick

// styles.css `.btn.btn-sm` (and `.btn-primary`). Hand-rolled rather than a
// QtQuick.Controls Button: the look is fixed by the Electron app's CSS, and
// restyling a Controls style to match costs more than these few lines.
Rectangle {
    id: button

    required property string text
    required property Theme theme
    property bool primary: false
    signal clicked()

    implicitWidth: label.implicitWidth + 20
    implicitHeight: 26
    radius: 6
    color: primary ? (mouse.containsMouse ? theme.accent : theme.accentStrong)
                   : (mouse.containsMouse ? theme.surfaceHover : theme.surface)
    border.width: 1
    border.color: primary ? color : (mouse.containsMouse ? theme.borderStrong : theme.border)
    Behavior on color { ColorAnimation { duration: 120 } }

    Text {
        id: label
        anchors.centerIn: parent
        text: button.text
        color: button.primary ? "white" : button.theme.text
        font.pointSize: 12
        font.weight: Font.Medium
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        onClicked: button.clicked()
    }
}
