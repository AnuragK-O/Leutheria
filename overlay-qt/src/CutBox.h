#pragma once

#include "CutGeometry.h"

#include <QColor>
#include <QQuickPaintedItem>
#include <QtQml/qqmlregistration.h>

// The design's panel and control shape, as one QML item: a fill with a 1 px
// outline, one corner cut at 45 degrees (the outline follows the diagonal),
// and optional corner brackets. The plate, the confirmation card, the
// buttons and the reticle's frame are all CutBoxes. Geometry is in
// CutGeometry (unit tested); this only paints it.
//
// Painted with QPainter (antialiased) into a texture at the window's device
// pixel ratio, so the hairlines and the diagonal are crisp at 1x and 2x. It
// repaints only when a property or the size changes.
//
//   CutBox { cut: theme.cutLg; color: theme.surface; lineColor: tone
//            brackets: CutBox.BottomBrackets; bracketColor: tone }
class CutBox : public QQuickPaintedItem
{
    Q_OBJECT
    QML_ELEMENT

    Q_PROPERTY(qreal cut READ cut WRITE setCut NOTIFY shapeChanged)
    Q_PROPERTY(Corner corner READ corner WRITE setCorner NOTIFY shapeChanged)
    Q_PROPERTY(QColor color READ color WRITE setColor NOTIFY shapeChanged)
    Q_PROPERTY(QColor lineColor READ lineColor WRITE setLineColor NOTIFY shapeChanged)
    Q_PROPERTY(qreal lineWidth READ lineWidth WRITE setLineWidth NOTIFY shapeChanged)
    // Brackets sit on the fill's corners (inside the outline), as in CSS
    // where they're drawn into the fill layer.
    Q_PROPERTY(Brackets brackets READ brackets WRITE setBrackets NOTIFY shapeChanged)
    Q_PROPERTY(QColor bracketColor READ bracketColor WRITE setBracketColor NOTIFY shapeChanged)
    Q_PROPERTY(qreal bracketArm READ bracketArm WRITE setBracketArm NOTIFY shapeChanged)
    Q_PROPERTY(qreal bracketThickness READ bracketThickness WRITE setBracketThickness NOTIFY shapeChanged)

public:
    enum Corner { TopRight, BottomRight };
    Q_ENUM(Corner)
    // The two uses the design has: the bottom corners (things that wait on
    // the user) and all four (the reticle's frame).
    enum Brackets { NoBrackets, BottomBrackets, AllBrackets };
    Q_ENUM(Brackets)

    explicit CutBox(QQuickItem *parent = nullptr);

    void paint(QPainter *painter) override;

    qreal cut() const { return m_cut; }
    Corner corner() const { return m_corner; }
    QColor color() const { return m_fill; }
    QColor lineColor() const { return m_line; }
    qreal lineWidth() const { return m_lineWidth; }
    Brackets brackets() const { return m_brackets; }
    QColor bracketColor() const { return m_bracketColor; }
    qreal bracketArm() const { return m_bracketArm; }
    qreal bracketThickness() const { return m_bracketThickness; }

    void setCut(qreal cut);
    void setCorner(Corner corner);
    void setColor(const QColor &color);
    void setLineColor(const QColor &color);
    void setLineWidth(qreal width);
    void setBrackets(Brackets brackets);
    void setBracketColor(const QColor &color);
    void setBracketArm(qreal arm);
    void setBracketThickness(qreal thickness);

signals:
    void shapeChanged();

private:
    template<typename T>
    void assign(T &member, const T &value);

    qreal m_cut = 0;
    Corner m_corner = TopRight;
    QColor m_fill = Qt::transparent;
    QColor m_line = Qt::transparent;
    qreal m_lineWidth = 1;
    Brackets m_brackets = NoBrackets;
    QColor m_bracketColor = Qt::transparent;
    qreal m_bracketArm = 12;     // --bracket
    qreal m_bracketThickness = 2; // --rule
};
