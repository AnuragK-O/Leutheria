import QtQuick

// Text laid out with a CSS line-height in px. Qt's FixedHeight lines put
// the glyphs at the top of the line box and all the extra height below;
// CSS splits it (half-leading above, half below). The shift is a transform,
// so it moves the glyphs without changing the item's size or layout.
Text {
    id: text

    required property real cssLineHeight

    lineHeightMode: Text.FixedHeight
    lineHeight: cssLineHeight

    FontMetrics {
        id: metrics
        font: text.font
    }
    transform: Translate { y: (text.cssLineHeight - metrics.height) / 2 }
}
