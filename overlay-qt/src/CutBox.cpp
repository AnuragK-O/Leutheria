#include "CutBox.h"

#include <QPainter>

CutBox::CutBox(QQuickItem *parent)
    : QQuickPaintedItem(parent)
{
    setAntialiasing(true);
    connect(this, &CutBox::shapeChanged, this, [this] { update(); });
}

void CutBox::paint(QPainter *painter)
{
    const QRectF box = boundingRect();
    if (box.isEmpty())
        return;
    const auto corner = m_corner == TopRight ? CutGeometry::Corner::TopRight : CutGeometry::Corner::BottomRight;

    painter->setRenderHint(QPainter::Antialiasing);
    painter->setPen(Qt::NoPen);
    // Outline layer, then the fill over it one line width in. The fill must
    // be opaque for the outline to read as a line (same rule as the CSS).
    if (m_lineWidth > 0 && m_line.alpha() > 0) {
        painter->setBrush(m_line);
        painter->drawPolygon(CutGeometry::outline(box, m_cut, corner));
    }
    if (m_fill.alpha() > 0) {
        painter->setBrush(m_fill);
        painter->drawPolygon(CutGeometry::fill(box, m_cut, corner, m_lineWidth > 0 ? m_lineWidth : 0));
    }
    if (m_brackets != NoBrackets && m_bracketColor.alpha() > 0) {
        const int corners = m_brackets == AllBrackets ? CutGeometry::TopLeft | CutGeometry::TopRight
                                                            | CutGeometry::BottomLeft | CutGeometry::BottomRight
                                                      : CutGeometry::BottomLeft | CutGeometry::BottomRight;
        const QRectF inner = box.adjusted(m_lineWidth, m_lineWidth, -m_lineWidth, -m_lineWidth);
        for (const QRectF &bar : CutGeometry::brackets(inner, corners, m_bracketArm, m_bracketThickness))
            painter->fillRect(bar, m_bracketColor);
    }
}

template<typename T>
void CutBox::assign(T &member, const T &value)
{
    if (member == value)
        return;
    member = value;
    emit shapeChanged();
}

void CutBox::setCut(qreal cut) { assign(m_cut, cut); }
void CutBox::setCorner(Corner corner) { assign(m_corner, corner); }
void CutBox::setFillColor(const QColor &color) { assign(m_fill, color); }
void CutBox::setLineColor(const QColor &color) { assign(m_line, color); }
void CutBox::setLineWidth(qreal width) { assign(m_lineWidth, width); }
void CutBox::setBrackets(Brackets brackets) { assign(m_brackets, brackets); }
void CutBox::setBracketColor(const QColor &color) { assign(m_bracketColor, color); }
void CutBox::setBracketArm(qreal arm) { assign(m_bracketArm, arm); }
void CutBox::setBracketThickness(qreal thickness) { assign(m_bracketThickness, thickness); }
