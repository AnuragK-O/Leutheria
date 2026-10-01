import QtQuick
import QtQuick.Effects
import QtQuick.Layouts
import Leutheria.Core

// The compact confirmation card under the island (overlay.css .ov-confirm).
// Approve / Decline only: the "remember" options stay in the main window's
// full card. Once answered -- here, by voice, or in the main window -- the
// buttons are replaced by the outcome, and the model hides the card shortly
// after.
Item {
    id: card

    required property OverlayModel overlay
    required property Theme theme

    visible: overlay.confirmationVisible
    implicitHeight: visible ? frame.implicitHeight : 0

    // @keyframes drop: fade in from 6px above.
    onVisibleChanged: if (visible) drop.restart()
    ParallelAnimation {
        id: drop
        NumberAnimation { target: card; property: "opacity"; from: 0; to: 1; duration: 240; easing.type: Easing.BezierSpline; easing.bezierCurve: card.theme.ease }
        NumberAnimation { target: shift; property: "y"; from: -6; to: 0; duration: 240; easing.type: Easing.BezierSpline; easing.bezierCurve: card.theme.ease }
    }
    transform: Translate { id: shift }

    RectangularShadow {
        anchors.fill: frame
        radius: frame.radius
        offset.y: 10
        blur: card.theme.shadowBlur
        color: card.theme.shadow
    }

    Rectangle {
        id: frame
        width: parent.width
        implicitHeight: content.implicitHeight + 2
        radius: 14
        color: card.theme.islandBg
        border.width: 1
        border.color: card.theme.mix(card.theme.warning, card.theme.border, 0.45)

        ColumnLayout {
            id: content
            x: 1
            y: 1
            width: parent.width - 2
            spacing: 0

            // header: warning icon + "Leutheria wants to run <tool>"
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: 12 * 1.5 + 16 // padding 8px 14px around one 12px line
                color: card.theme.warningSoft
                topLeftRadius: 13
                topRightRadius: 13

                RowLayout {
                    id: head
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    spacing: 8
                    Icon { name: "warning"; color: card.theme.warning }
                    Text {
                        Layout.fillWidth: true
                        text: qsTr("Leutheria wants to run %1").arg(card.overlay.confirmationTool)
                        color: card.theme.warning
                        font.pointSize: 12
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                        lineHeightMode: Text.FixedHeight
                        lineHeight: 12 * 1.5
                    }
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 14
                Layout.rightMargin: 14
                Layout.topMargin: 10
                Layout.bottomMargin: 12
                spacing: 10

                // the call, as a code block, at most two lines
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: command.implicitHeight + 18
                    radius: 6
                    color: card.theme.bg
                    border.width: 1
                    border.color: card.theme.border

                    Text {
                        id: command
                        x: 11
                        y: 9
                        width: parent.width - 22
                        text: card.overlay.confirmationCommand
                        color: card.theme.text
                        font.family: card.theme.monoFamily
                        font.pointSize: 11.5
                        lineHeightMode: Text.FixedHeight
                        lineHeight: 11.5 * 1.5
                        wrapMode: Text.WrapAnywhere
                        maximumLineCount: 2
                        elide: Text.ElideRight
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8

                    Text {
                        Layout.fillWidth: true
                        text: card.overlay.confirmationAnswered ? "" : qsTr("Say yes or no, or choose")
                        color: card.theme.textMuted
                        font.pointSize: 11.5
                        elide: Text.ElideRight
                    }

                    // Unanswered: the two buttons.
                    OverlayButton {
                        visible: !card.overlay.confirmationAnswered
                        text: qsTr("Decline")
                        theme: card.theme
                        onClicked: card.overlay.answer(false)
                    }
                    OverlayButton {
                        visible: !card.overlay.confirmationAnswered
                        text: qsTr("Approve")
                        theme: card.theme
                        primary: true
                        onClicked: card.overlay.answer(true)
                    }

                    // Answered: what was decided, and where.
                    RowLayout {
                        visible: card.overlay.confirmationAnswered
                        Layout.preferredHeight: 26
                        spacing: 6
                        Icon {
                            name: card.overlay.confirmationApproved ? "check" : "x"
                            color: card.theme.textMuted
                        }
                        Text {
                            text: card.overlay.confirmationOutcome
                            color: card.theme.textMuted
                            font.pointSize: 12
                            font.weight: Font.Medium
                        }
                    }
                }
            }
        }
    }
}
