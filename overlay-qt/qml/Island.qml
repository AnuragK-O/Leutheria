import QtQuick
import QtQuick.Effects
import QtQuick.Layouts
import Leutheria.Core

// The pill: asset slot, state label, one (at most two) lines of text.
// overlay.css .island / .island-label / .island-line.
Item {
    id: island

    required property OverlayModel overlay
    required property AssetManifest manifest
    required property Theme theme

    readonly property bool confirming: overlay.state === "awaiting_confirmation"
    readonly property int slotSize: manifest.slotSize

    // content + 10px padding top and bottom + the 1px border (CSS content-box)
    implicitHeight: Math.max(slotSize, textColumn.implicitHeight) + 22

    // box-shadow: two layers in light mode, one in dark.
    RectangularShadow {
        anchors.fill: pill
        radius: pill.radius
        offset.y: 10
        blur: island.theme.shadowBlur
        color: island.theme.shadow
    }
    RectangularShadow {
        anchors.fill: pill
        visible: !island.theme.dark
        radius: pill.radius
        offset.y: 1
        blur: 3
        color: Qt.rgba(25 / 255, 19 / 255, 35 / 255, 0.08)
    }

    Rectangle {
        id: pill
        anchors.fill: parent
        radius: (island.slotSize + 20) / 2
        color: island.theme.islandBg
        border.width: 1
        border.color: island.confirming ? island.theme.mix(island.theme.warning, "transparent", 0.5)
                                        : island.theme.islandBorder
        Behavior on border.color { ColorAnimation { duration: 300 } }
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 18
        spacing: 14

        AssetSlot {
            Layout.preferredWidth: island.slotSize
            Layout.preferredHeight: island.slotSize
            visualState: island.overlay.state
            level: island.overlay.level
            manifest: island.manifest
            theme: island.theme
        }

        ColumnLayout {
            id: textColumn
            Layout.fillWidth: true
            spacing: 1

            Text {
                Layout.fillWidth: true
                text: island.overlay.label.toUpperCase()
                color: island.confirming ? island.theme.warning : island.theme.accent
                font.pointSize: 10.5
                font.weight: Font.DemiBold
                font.letterSpacing: 10.5 * 0.08
                lineHeightMode: Text.FixedHeight
                lineHeight: 10.5 * 1.5 // the app's body line-height
                elide: Text.ElideRight
            }

            Text {
                Layout.fillWidth: true
                text: island.overlay.line
                color: island.overlay.lineMuted ? island.theme.textMuted : island.theme.text
                font.pointSize: 13.5
                lineHeightMode: Text.FixedHeight
                lineHeight: 13.5 * 1.4 // CSS line-height is px, not Qt's "proportional"
                wrapMode: Text.Wrap
                maximumLineCount: 2
                elide: Text.ElideRight
            }
        }
    }
}
