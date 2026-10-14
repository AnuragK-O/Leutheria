#pragma once

#include <QList>
#include <QPolygonF>
#include <QRectF>

// The geometry of the design's cut corner and corner brackets
// (app/renderer/styles.css "primitive: the cut corner"; DESIGN.md "Space and
// geometry"), as plain maths so it can be tested without a window. CutBox
// draws it.
//
// CSS can't stroke a clipped edge (clip-path cuts the border off along the
// diagonal), so the outline is two stacked shapes: the outline colour, then
// the fill, inset by the line width. Here as well, which keeps the diagonal
// exactly as thick as the straight edges.
namespace CutGeometry {

// Which corner loses its square. Controls cut bottom-right; panels top-right.
enum class Corner { TopRight, BottomRight };

// The outer shape: `rect` with `corner` cut at 45 degrees, `cut` px along
// each edge. A cut of 0 is the plain rectangle (4 points). The cut is
// clamped to the shorter side.
QPolygonF outline(const QRectF &rect, qreal cut, Corner corner);

// The fill: `rect` inset by `line` on every side, its cut shrunk by
// (2 - sqrt 2) * line so that the band between the two diagonals is `line`
// thick measured perpendicular to it -- the 0.59 px in styles.css for a
// 1 px hairline. line == 0 gives outline() itself.
QPolygonF fill(const QRectF &rect, qreal cut, Corner corner, qreal line);

// The perpendicular distance between the outline's diagonal and the fill's.
// For the tests: equal to `line` for any cut that leaves a diagonal.
qreal diagonalBand(const QRectF &rect, qreal cut, Corner corner, qreal line);

enum BracketCorner {
    TopLeft = 0x1,
    TopRight = 0x2,
    BottomLeft = 0x4,
    BottomRight = 0x8,
};

// Corner brackets: per requested corner, two `arm` x `thickness` bars
// meeting at the corner of `rect` (a horizontal one, then a vertical one).
// The bars overlap in the corner square, as CSS's stacked gradients do.
QList<QRectF> brackets(const QRectF &rect, int corners, qreal arm, qreal thickness);

} // namespace CutGeometry
