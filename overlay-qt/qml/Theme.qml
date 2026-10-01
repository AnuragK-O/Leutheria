import QtQuick

// The overlay's palette: the brand tokens from app/renderer/styles.css plus
// the overlay-only ones from app/renderer/overlay/overlay.css, per theme.
// Kept as literal values (not computed) so a diff against the CSS is easy.
QtObject {
    property bool dark: true

    // island + card surface
    readonly property color islandBg: dark ? Qt.rgba(23 / 255, 19 / 255, 31 / 255, 0.9) : Qt.rgba(1, 1, 1, 0.94)
    readonly property color islandBorder: dark ? Qt.rgba(166 / 255, 139 / 255, 240 / 255, 0.22)
                                               : Qt.rgba(120 / 255, 86 / 255, 196 / 255, 0.2)
    readonly property color shadow: dark ? Qt.rgba(0, 0, 0, 0.45) : Qt.rgba(25 / 255, 19 / 255, 35 / 255, 0.16)
    readonly property real shadowBlur: dark ? 34 : 30

    // text
    readonly property color text: dark ? "#ede9f4" : "#191323"
    readonly property color textMuted: dark ? "#a79fb8" : "#6b6379"

    // brand
    readonly property color accent: dark ? "#a68bf0" : "#7856c4"         // --violet-400 / 600
    readonly property color accentStrong: dark ? "#8b6bd6" : "#63449f"   // --violet-500 / 700
    readonly property color warning: dark ? "#e0b25c" : "#a97a1c"
    readonly property color warningSoft: dark ? Qt.rgba(224 / 255, 178 / 255, 92 / 255, 0.14)
                                              : Qt.rgba(169 / 255, 122 / 255, 28 / 255, 0.13)

    // surfaces used by the card's code block and buttons
    readonly property color bg: dark ? "#131019" : "#f6f4fa"
    readonly property color surface: dark ? "#1c1726" : "#ffffff"
    readonly property color surfaceHover: dark ? "#221c2e" : "#f4f1f9"
    readonly property color border: dark ? "#2b2338" : "#e5e0ee"
    readonly property color borderStrong: dark ? "#3a3049" : "#d2cae1"

    // the placeholder orb (violet); Orb.qml swaps to amber for confirmations
    readonly property color orbCore: dark ? "#b9a3f5" : "#a68bf0"
    readonly property color orbMid: dark ? "#8b6bd6" : "#7856c4"
    readonly property color orbEdge: "#63449f"
    readonly property color ring: dark ? Qt.rgba(166 / 255, 139 / 255, 240 / 255, 0.55)
                                       : Qt.rgba(120 / 255, 86 / 255, 196 / 255, 0.45)

    readonly property color amberCore: "#f3d596"
    readonly property color amberEdge: "#9a6c17"
    readonly property color amberRing: Qt.rgba(warning.r, warning.g, warning.b, 0.6)

    // CSS color-mix(in srgb, a p%, b) -- used for the amber borders. CSS
    // interpolates premultiplied, so mixing with "transparent" fades the
    // colour instead of darkening it towards black.
    function mix(a: color, b: color, p: real): color {
        const wa = a.a * p, wb = b.a * (1 - p), alpha = wa + wb
        if (alpha <= 0)
            return Qt.rgba(0, 0, 0, 0)
        return Qt.rgba((a.r * wa + b.r * wb) / alpha, (a.g * wa + b.g * wb) / alpha,
                       (a.b * wa + b.b * wb) / alpha, alpha)
    }

    // CSS asks for ui-monospace (SF Mono); Qt can't resolve that by name,
    // so main.cpp passes the system fixed-pitch family in.
    property string monoFamily: "Menlo"
    // --ease: cubic-bezier(0.32, 0.72, 0, 1)
    readonly property var ease: [0.32, 0.72, 0, 1, 1, 1]
}
