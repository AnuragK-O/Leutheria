pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes

// The three stroke icons the card uses, drawn from the same 24x24 SVG paths
// as app/renderer/js/util.js icon(), so they match the Electron card exactly.
Item {
    id: icon

    required property string name
    property color color: "black"

    // Up to three sub-paths per icon, as in util.js ("a|b|c").
    readonly property var paths: ({
        check: ["M20 6 9 17l-5-5"],
        x: ["M18 6 6 18", "M6 6l12 12"],
        warning: ["m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z", "M12 9v4", "M12 17h.01"],
    })[name] ?? []

    implicitWidth: 14
    implicitHeight: 14

    // Drawn in the SVG's own 24x24 space and scaled as a whole, so the stroke
    // scales with it just like `stroke-width="1.7"` in a viewBox does.
    Shape {
        width: 24
        height: 24
        scale: icon.width / 24
        transformOrigin: Item.TopLeft
        preferredRendererType: Shape.CurveRenderer

        component Stroke: ShapePath {
            property alias d: svg.path
            fillColor: "transparent"
            strokeColor: icon.color
            strokeWidth: 1.7
            capStyle: ShapePath.RoundCap
            joinStyle: ShapePath.RoundJoin
            PathSvg { id: svg }
        }

        Stroke { d: icon.paths[0] ?? "" }
        Stroke { d: icon.paths[1] ?? "" }
        Stroke { d: icon.paths[2] ?? "" }
    }
}
