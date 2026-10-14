import QtQuick
import QtQuick.Layouts
import Leutheria.Core

// The compact confirmation card under the plate (overlay.css .ov-confirm;
// DESIGN.md "Confirmation card"). Approve / Decline only: the "remember"
// options stay in the main window's full card. Once answered -- here, by
// voice, in the main window, or by the agent's timeout -- the buttons are
// replaced by the outcome, the amber leaves with the question (the card
// goes neutral), and the model hides the card shortly after.
Item {
    id: card

    required property OverlayModel overlay
    required property Theme theme

    readonly property bool answered: overlay.confirmationAnswered
    readonly property bool clickOnly: !overlay.confirmationVoiceApprovable && !answered
    // --tone: amber while open, --text-3 once answered (.ov-confirm.resolved)
    readonly property color tone: answered ? theme.text3 : theme.amber

    visible: overlay.confirmationVisible
    implicitHeight: visible ? content.implicitHeight : 0

    // @keyframes drop: from 6 px above, transparent; --t-base --ease.
    onVisibleChanged: if (visible) drop.restart()
    ParallelAnimation {
        id: drop
        NumberAnimation { target: card; property: "opacity"; from: 0; to: 1; duration: card.theme.tBase; easing.type: Easing.BezierSpline; easing.bezierCurve: card.theme.ease }
        NumberAnimation { target: shift; property: "y"; from: -6; to: 0; duration: card.theme.tBase; easing.type: Easing.BezierSpline; easing.bezierCurve: card.theme.ease }
    }
    transform: Translate { id: shift }

    CutBox {
        anchors.fill: parent
        cut: card.theme.cutMd
        color: card.theme.surface
        lineColor: card.theme.mix(card.tone, card.theme.line, 0.6)
        brackets: CutBox.BottomBrackets
        bracketColor: card.tone
        bracketArm: card.theme.bracket
        bracketThickness: card.theme.rule
    }

    ColumnLayout {
        id: content
        width: parent.width
        spacing: 0

        // .ov-confirm-head: 30 px including its rule; warning icon and the
        // amber micro-label.
        Item {
            Layout.fillWidth: true
            implicitHeight: 30

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 14
                anchors.rightMargin: card.theme.cutMd + 10
                anchors.bottomMargin: card.theme.hair
                spacing: card.theme.s2
                Icon {
                    name: "warning"
                    color: card.tone
                    implicitWidth: 13
                    implicitHeight: 13
                }
                CssText {
                    Layout.fillWidth: true
                    text: qsTr("Approval required").toUpperCase()
                    color: card.tone
                    font: Typeface.mono(500, card.theme.fsMicro, card.theme.trackLabel)
                    cssLineHeight: card.theme.fsMicro * 1.4
                    elide: Text.ElideRight
                }
            }
            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: card.theme.hair
                color: card.theme.mix(card.tone, card.theme.line, 0.35)
            }
        }

        // .ov-confirm-body: padding 10 14 14, 10 px between rows.
        ColumnLayout {
            Layout.fillWidth: true
            Layout.leftMargin: 14
            Layout.rightMargin: 14
            Layout.topMargin: 10
            Layout.bottomMargin: 14
            spacing: 10

            // .code-block.ov-confirm-cmd: the call, at most two lines.
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: command.implicitHeight + 18 + 2 * card.theme.hair
                color: card.theme.sunken
                border.width: card.theme.hair
                border.color: card.theme.line

                CssText {
                    id: command
                    x: 12 + card.theme.hair
                    y: 9 + card.theme.hair
                    width: parent.width - 2 * x
                    text: card.overlay.confirmationCommand
                    color: card.theme.text
                    font: Typeface.mono(400, card.theme.fsSm)
                    cssLineHeight: card.theme.fsSm * 1.55
                    wrapMode: Text.WrapAnywhere
                    maximumLineCount: 2
                    elide: Text.ElideRight
                }
            }

            // .ov-confirm-reason: why this one asks, one line.
            CssText {
                Layout.fillWidth: true
                visible: text !== ""
                text: card.overlay.confirmationReason
                color: card.theme.text2
                font: Typeface.ui(400, card.theme.fsSm)
                cssLineHeight: card.theme.fsSm * 1.4
                elide: Text.ElideRight
                maximumLineCount: 1
            }

            // .ov-confirm-foot
            RowLayout {
                Layout.fillWidth: true
                Layout.minimumHeight: card.theme.hit
                spacing: card.theme.s2

                // The hint: "say yes or no", or, when a spoken yes won't do,
                // an amber state with its indicator (.click-only).
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 7
                    Rectangle {
                        visible: card.clickOnly
                        implicitWidth: 6 // --led
                        implicitHeight: 6
                        color: card.theme.amber
                    }
                    CssText {
                        Layout.fillWidth: true
                        text: card.answered ? ""
                            : card.clickOnly ? qsTr("Needs a click").toUpperCase()
                            : qsTr("Say yes or no, or choose")
                        color: card.clickOnly ? card.theme.amber : card.theme.text2
                        font: card.clickOnly ? Typeface.mono(500, card.theme.fsMicro, card.theme.trackLabel)
                                             : Typeface.ui(400, card.theme.fsSm)
                        cssLineHeight: card.clickOnly ? card.theme.fsMicro * 1.4 : card.theme.fsSm * 1.5
                        elide: Text.ElideRight
                    }
                }

                // Unanswered: Decline (outlined red), then Approve (amber).
                OverlayButton {
                    visible: !card.answered
                    text: qsTr("Decline")
                    theme: card.theme
                    kind: OverlayButton.Danger
                    onClicked: card.overlay.answer(false)
                }
                OverlayButton {
                    visible: !card.answered
                    text: qsTr("Approve")
                    theme: card.theme
                    kind: OverlayButton.Hot
                    onClicked: card.overlay.answer(true)
                }

                // Answered: what was decided, and where (.ov-confirm-resolved).
                RowLayout {
                    visible: card.answered
                    spacing: card.theme.s2
                    Icon {
                        name: card.overlay.confirmationApproved ? "check" : "x"
                        color: card.overlay.confirmationApproved ? card.theme.green : card.theme.red
                        implicitWidth: 12
                        implicitHeight: 12
                    }
                    CssText {
                        text: card.overlay.confirmationOutcome
                        color: card.theme.text2
                        font: Typeface.mono(400, card.theme.fsXs)
                        cssLineHeight: card.theme.fsXs * 1.5
                    }
                }
            }
        }
    }
}
