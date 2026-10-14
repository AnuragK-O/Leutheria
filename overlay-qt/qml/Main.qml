pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Effects
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
    // Set by main.cpp but no longer used: the mono face is the bundled
    // JetBrains Mono (Typeface). Drop it together with main.cpp's
    // setInitialProperties entry.
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
        dark: root.forcedTheme !== "" ? root.forcedTheme === "dark"
                                      : Application.styleHints.colorScheme === Qt.ColorScheme.Dark
    }

    // Everything visible. Arrives (opacity 0 -> 1, 8 px down) when a session
    // starts and leaves the reverse way when it ends, --t-slow --ease; C++
    // keeps the window mapped for OverlayModel::kLingerMs so the leave can
    // finish (overlay.css .stage, body.leaving .stage).
    Item {
        id: stage

        readonly property bool shown: root.overlay.inSession

        x: (root.width - width) / 2
        y: 10
        width: 420
        height: column.implicitHeight
        opacity: shown ? 1 : 0
        transform: Translate {
            y: stage.shown ? 0 : -8
            Behavior on y { NumberAnimation { duration: theme.tSlow; easing.type: Easing.BezierSpline; easing.bezierCurve: theme.ease } }
        }
        Behavior on opacity { NumberAnimation { duration: theme.tSlow; easing.type: Easing.BezierSpline; easing.bezierCurve: theme.ease } }

        // The product's one shadow, drop-shadow(0 8px 18px rgba(0,0,0,.4)),
        // on the whole stage so it follows the cut corners like CSS's does.
        layer.enabled: true
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: theme.shadow
            shadowVerticalOffset: theme.shadowY
            shadowHorizontalOffset: 0
            blurMax: 64
            shadowBlur: 1
            autoPaddingEnabled: true
        }

        Column {
            id: column
            width: parent.width
            spacing: theme.s2

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
