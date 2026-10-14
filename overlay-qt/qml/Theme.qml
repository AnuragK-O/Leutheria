import QtQuick

// The design tokens the overlay uses, for both themes.
//
// Source: app/renderer/DESIGN.md (the token tables), whose values are the
// first two blocks of app/renderer/styles.css. Each property is the CSS
// custom property of the same name in camelCase (--line-strong -> lineStrong,
// --t-fast -> tFast), written as the same literal, so a diff against either
// file stays easy. Nothing here is computed except where the CSS computes it
// too (color-mix, the per-state tone). Change a value there first, then here.
QtObject {
    id: theme

    property bool dark: true

    // --- colour: ground ----------------------------------------------------
    readonly property color base: dark ? "#0a0810" : "#efedf4"        // --base
    readonly property color sunken: dark ? "#07060b" : "#e5e2ed"      // --sunken: code wells
    readonly property color surface: dark ? "#110e19" : "#f8f7fb"     // --surface: the plate, the card
    readonly property color raised: dark ? "#191524" : "#ffffff"      // --raised: controls
    readonly property color raised2: dark ? "#231d31" : "#e9e4f4"     // --raised-2: hovered controls
    readonly property color line: dark ? "#272138" : "#d2cce0"        // --line: hairlines
    readonly property color lineStrong: dark ? "#3d3556" : "#aea6c4"  // --line-strong: control outlines

    // --- colour: text ------------------------------------------------------
    readonly property color text: dark ? "#ede9f6" : "#15111f"        // --text
    readonly property color text2: dark ? "#aba3bf" : "#4c4561"       // --text-2: hints, outcomes
    readonly property color text3: dark ? "#9088a9" : "#655e7c"       // --text-3: labels, meta

    // --- colour: brand and state. Colour means state, never decoration. ----
    readonly property color violet: dark ? "#8e6fd4" : "#7353c9"      // --violet
    readonly property color violetHi: dark ? "#b29af7" : "#5a3bb0"    // --violet-hi: working, talking
    readonly property color violetLo: dark ? "#6c4dc2" : "#6444bd"    // --violet-lo
    readonly property color textOnViolet: "#ffffff"                   // --on-violet
    readonly property color cyan: dark ? "#38e1ee" : "#006f7c"        // --cyan: live (the mic is open)
    readonly property color amber: dark ? "#ffb224" : "#8a5000"       // --amber: waiting on you
    readonly property color red: dark ? "#ff5c7a" : "#b81d43"         // --red: declined, destructive
    readonly property color green: dark ? "#5be49b" : "#156c3c"       // --green: succeeded
    readonly property color textOnAccent: dark ? "#0a0810" : "#ffffff" // --on-accent (QML reserves on*): text on an amber fill

    // --- type sizes (CSS px) and tracking (em) -----------------------------
    readonly property int fsMicro: 10       // --fs-micro: uppercase labels
    readonly property int fsXs: 11          // --fs-xs: mono meta, small buttons
    readonly property int fsSm: 12          // --fs-sm: secondary text, mono data
    readonly property int fsMd: 13          // --fs-md: body
    readonly property real trackLabel: 0.12 // --track-label: micro-labels
    readonly property real trackButton: 0.06 // .btn letter-spacing

    // --- space: the 4 px scale ---------------------------------------------
    readonly property int s1: 4
    readonly property int s2: 8
    readonly property int s3: 12
    readonly property int s4: 16

    // --- geometry. Nothing is rounded. -------------------------------------
    readonly property int hair: 1     // --hair: every border
    readonly property int rule: 2     // --rule: state rules, bracket thickness
    readonly property int cutSm: 6    // --cut-sm: buttons (bottom-right)
    readonly property int cutMd: 10   // --cut-md: the confirmation card (top-right)
    readonly property int cutLg: 14   // --cut-lg: the plate (top-right)
    readonly property int bracket: 12 // --bracket: corner bracket arm
    readonly property int hit: 28     // --hit: small buttons, the card's footer row

    // --- motion ------------------------------------------------------------
    readonly property int tFast: 90   // --t-fast: hover, press, level-driven movement
    readonly property int tBase: 160  // --t-base: state changes, the card arriving
    readonly property int tSlow: 320  // --t-slow: the overlay arriving and leaving
    // --ease: cubic-bezier(0.2, 0.8, 0.2, 1), as Easing.BezierSpline wants it
    readonly property var ease: [0.2, 0.8, 0.2, 1, 1, 1]

    // --- the product's one shadow: drop-shadow(0 8px 18px rgba(0,0,0,.4)) ---
    readonly property color shadow: Qt.rgba(0, 0, 0, 0.4)
    readonly property int shadowY: 8
    readonly property int shadowBlur: 18

    // --tone: the colour of a voice state (overlay.css .island[data-state]).
    // Cyan while the mic is live, violet while Leutheria is working or
    // talking, amber when it needs an answer.
    function tone(state: string): color {
        switch (state) {
        case "transcribing":
        case "thinking":
        case "speaking":
            return violetHi
        case "awaiting_confirmation":
            return amber
        default: // listening, hearing
            return cyan
        }
    }

    // CSS color-mix(in srgb, a p, b), p as 0..1. Tints are mixed from tokens
    // in place, never written as new literals, so they follow the theme.
    // CSS interpolates premultiplied, so mixing with "transparent" fades a
    // colour instead of darkening it.
    function mix(a: color, b: color, p: real): color {
        const wa = a.a * p, wb = b.a * (1 - p), alpha = wa + wb
        if (alpha <= 0)
            return Qt.rgba(0, 0, 0, 0)
        return Qt.rgba((a.r * wa + b.r * wb) / alpha, (a.g * wa + b.g * wb) / alpha,
                       (a.b * wa + b.b * wb) / alpha, alpha)
    }

}
