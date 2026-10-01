import QtQuick
import Leutheria.Core

// The overlay window. Pixels only: every decision about what to show comes
// from `overlay` (OverlayModel), every asset from `manifest` (AssetManifest).
// Showing, hiding, placing and click-through are done from C++ (main.cpp +
// MacWindow_mac.mm), which is why `visible` is never bound here.
Window {
    id: root

    required property OverlayModel overlay
    required property AssetManifest manifest
    // "light" / "dark" from --theme, or "" to follow the macOS appearance.
    required property string forcedTheme
    // QFontDatabase::systemFont(FixedFont), for the card's code block.
    required property string monoFamily

    // Same size as the Electron overlay window (main.js OVERLAY_SIZE): room
    // for the island plus the confirmation card under it.
    width: 460
    height: 300
    color: "transparent"
    title: qsTr("Leutheria overlay")
    // Qt::Tool gives an NSPanel (the only kind of window that can take a click
    // without activating its app); WindowDoesNotAcceptFocus keeps it from
    // ever becoming key. MacWindow_mac.mm does the rest.
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
           | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint

    Theme {
        id: theme
        monoFamily: root.monoFamily
        dark: root.forcedTheme !== "" ? root.forcedTheme === "dark"
                                      : Application.styleHints.colorScheme === Qt.ColorScheme.Dark
    }

    // Everything visible. Fades/drops in when a session starts and back out
    // when it ends; C++ keeps the window mapped for OverlayModel::kLingerMs
    // so the fade-out can finish (overlay.css body.leaving .stage).
    Item {
        id: stage

        readonly property bool shown: root.overlay.inSession

        x: (root.width - width) / 2
        y: 10
        width: 420
        height: column.implicitHeight
        opacity: shown ? 1 : 0
        transform: [
            Scale {
                origin.x: stage.width / 2
                origin.y: stage.height / 2
                xScale: stage.shown ? 1 : 0.97
                yScale: xScale
                Behavior on xScale { NumberAnimation { duration: 400; easing.type: Easing.BezierSpline; easing.bezierCurve: theme.ease } }
            },
            Translate {
                y: stage.shown ? 0 : -8
                Behavior on y { NumberAnimation { duration: 400; easing.type: Easing.BezierSpline; easing.bezierCurve: theme.ease } }
            }
        ]
        Behavior on opacity { NumberAnimation { duration: 400; easing.type: Easing.BezierSpline; easing.bezierCurve: theme.ease } }

        Column {
            id: column
            width: parent.width
            spacing: 8

            Island {
                width: parent.width
                overlay: root.overlay
                manifest: root.manifest
                theme: theme
            }

            ConfirmCard {
                width: parent.width
                overlay: root.overlay
                theme: theme
            }
        }
    }
}
