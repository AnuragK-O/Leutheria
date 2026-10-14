import QtQuick
import QtQuick.Layouts
import Leutheria.Core

// The plate: asset slot, a hairline divider, the state label over one (at
// most two) lines of text. overlay.css .island / .island-text /
// .island-label / .island-line; DESIGN.md "Plate".
Item {
    id: island

    required property OverlayModel overlay
    required property AssetManifest manifest
    required property Theme theme

    readonly property int slotSize: manifest.slotSize
    // --tone: cyan while the mic is live, violet while working or talking,
    // amber when it needs an answer.
    readonly property color tone: theme.tone(overlay.state)

    // padding: 10px (cut + 6px) 10px 10px; min-height: slot + 20px
    readonly property int padding: 10
    implicitHeight: Math.max(slotSize, textColumn.implicitHeight) + 2 * padding

    CutBox {
        anchors.fill: parent
        cut: island.theme.cutLg
        color: island.theme.surface
        lineColor: island.theme.mix(island.tone, island.theme.lineStrong, 0.55)
        Behavior on lineColor { ColorAnimation { duration: island.theme.tFast } }
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: island.padding
        anchors.rightMargin: island.theme.cutLg + 6
        spacing: island.theme.s3

        AssetSlot {
            Layout.preferredWidth: island.slotSize
            Layout.preferredHeight: island.slotSize
            visualState: island.overlay.state
            level: island.overlay.level
            manifest: island.manifest
            theme: island.theme
        }

        // .island-text: padding-left 12px behind a 1px --line-strong rule,
        // stretched to the row's height.
        Rectangle {
            Layout.fillHeight: true
            implicitWidth: island.theme.hair
            color: island.theme.lineStrong
        }

        ColumnLayout {
            id: textColumn
            Layout.fillWidth: true
            spacing: 2

            Text {
                Layout.fillWidth: true
                text: island.overlay.label.toUpperCase()
                color: island.tone
                font: Typeface.mono(500, island.theme.fsMicro, island.theme.trackLabel)
                lineHeightMode: Text.FixedHeight
                lineHeight: island.theme.fsMicro * 1.4
                elide: Text.ElideRight
                Behavior on color { ColorAnimation { duration: island.theme.tBase } }
            }

            Text {
                Layout.fillWidth: true
                text: island.overlay.line
                color: island.overlay.lineMuted ? island.theme.text2 : island.theme.text
                font: Typeface.ui(400, island.theme.fsMd)
                // CSS line-height is px; Qt's default mode is proportional.
                lineHeightMode: Text.FixedHeight
                lineHeight: island.theme.fsMd * 1.4
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }
        }
    }
}
