#include "CutGeometry.h"

#include <QLineF>

#include <algorithm>
#include <cmath>

namespace CutGeometry {

QPolygonF outline(const QRectF &rect, qreal cut, Corner corner)
{
    const qreal c = std::clamp(cut, 0.0, std::min(rect.width(), rect.height()));
    const qreal l = rect.left(), t = rect.top(), r = rect.right(), b = rect.bottom();
    if (c <= 0)
        return QPolygonF({{l, t}, {r, t}, {r, b}, {l, b}});
    if (corner == Corner::TopRight)
        return QPolygonF({{l, t}, {r - c, t}, {r, t + c}, {r, b}, {l, b}});
    return QPolygonF({{l, t}, {r, t}, {r, b - c}, {r - c, b}, {l, b}});
}

QPolygonF fill(const QRectF &rect, qreal cut, Corner corner, qreal line)
{
    const QRectF inner = rect.adjusted(line, line, -line, -line);
    if (inner.isEmpty())
        return {};
    // A cut smaller than the shrink has no diagonal left inside the line.
    const qreal shrunk = std::max(0.0, cut - (2.0 - std::sqrt(2.0)) * line);
    return outline(inner, shrunk, corner);
}

qreal diagonalBand(const QRectF &rect, qreal cut, Corner corner, qreal line)
{
    const QPolygonF outer = outline(rect, cut, corner);
    const QPolygonF inner = fill(rect, cut, corner, line);
    if (outer.size() != 5 || inner.size() != 5)
        return 0;
    // The diagonal is the edge from point 1 to 2 (top-right) or 2 to 3.
    const int i = corner == Corner::TopRight ? 1 : 2;
    const QLineF diagonal(outer[i], outer[i + 1]);
    // Distance from a point on the inner diagonal to the outer one's line.
    const QPointF p = inner[i];
    const QPointF d = diagonal.p2() - diagonal.p1();
    const qreal cross = d.x() * (p.y() - diagonal.p1().y()) - d.y() * (p.x() - diagonal.p1().x());
    return std::abs(cross) / diagonal.length();
}

QList<QRectF> brackets(const QRectF &rect, int corners, qreal arm, qreal thickness)
{
    QList<QRectF> bars;
    const qreal a = std::min({arm, rect.width(), rect.height()});
    const qreal t = std::min(thickness, a);
    const qreal l = rect.left(), top = rect.top(), r = rect.right(), b = rect.bottom();
    if (corners & TopLeft)
        bars << QRectF(l, top, a, t) << QRectF(l, top, t, a);
    if (corners & TopRight)
        bars << QRectF(r - a, top, a, t) << QRectF(r - t, top, t, a);
    if (corners & BottomLeft)
        bars << QRectF(l, b - t, a, t) << QRectF(l, b - a, t, a);
    if (corners & BottomRight)
        bars << QRectF(r - a, b - t, a, t) << QRectF(r - t, b - a, t, a);
    return bars;
}

} // namespace CutGeometry
