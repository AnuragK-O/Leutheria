// The cut corner and bracket geometry CutBox paints (CutGeometry).

#include "CutGeometry.h"

#include <QTest>

#include <cmath>

using CutGeometry::Corner;

class TestCutGeometry : public QObject
{
    Q_OBJECT

private slots:
    void outlineCutsTheRequestedCorner()
    {
        const QRectF r(0, 0, 420, 76);
        QCOMPARE(CutGeometry::outline(r, 14, Corner::TopRight),
                 QPolygonF({{0, 0}, {406, 0}, {420, 14}, {420, 76}, {0, 76}}));
        QCOMPARE(CutGeometry::outline(r, 6, Corner::BottomRight),
                 QPolygonF({{0, 0}, {420, 0}, {420, 70}, {414, 76}, {0, 76}}));
    }

    void noCutIsARectangle()
    {
        QCOMPARE(CutGeometry::outline(QRectF(0, 0, 10, 10), 0, Corner::TopRight).size(), 4);
    }

    void cutIsClampedToTheShorterSide()
    {
        const QPolygonF p = CutGeometry::outline(QRectF(0, 0, 100, 8), 14, Corner::TopRight);
        QCOMPARE(p.at(1), QPointF(92, 0));
        QCOMPARE(p.at(2), QPointF(100, 8));
    }

    // The CSS fill layer: inset one hairline, cut + 0.59 px (2 - sqrt 2).
    void fillMatchesTheCss()
    {
        const QPolygonF p = CutGeometry::fill(QRectF(0, 0, 420, 76), 14, Corner::TopRight, 1);
        const qreal c = 14 - (2 - std::sqrt(2.0));
        QCOMPARE(p.at(0), QPointF(1, 1));
        QCOMPARE(p.at(1), QPointF(419 - c, 1));
        QCOMPARE(p.at(2), QPointF(419, 1 + c));
        QCOMPARE(p.at(3), QPointF(419, 75));
        QCOMPARE(p.at(4), QPointF(1, 75));
    }

    // The point of the shrink: the outline is as thick on the diagonal as on
    // the straight edges, for any line width and either corner.
    void diagonalIsAsThickAsTheEdges_data()
    {
        QTest::addColumn<qreal>("cut");
        QTest::addColumn<qreal>("line");
        QTest::addColumn<int>("corner");
        for (const qreal cut : {6.0, 10.0, 14.0})
            for (const qreal line : {1.0, 2.0})
                for (const int corner : {0, 1})
                    QTest::addRow("cut %g line %g corner %d", cut, line, corner) << cut << line << corner;
    }

    void diagonalIsAsThickAsTheEdges()
    {
        QFETCH(qreal, cut);
        QFETCH(qreal, line);
        QFETCH(int, corner);
        const qreal band = CutGeometry::diagonalBand(QRectF(0, 0, 200, 60), cut, Corner(corner), line);
        QVERIFY2(std::abs(band - line) < 1e-9, qPrintable(QString::number(band)));
    }

    void zeroLineFillIsTheOutline()
    {
        const QRectF r(0, 0, 50, 30);
        QCOMPARE(CutGeometry::fill(r, 10, Corner::BottomRight, 0), CutGeometry::outline(r, 10, Corner::BottomRight));
    }

    void bracketsOnTheBottomCorners()
    {
        // The card's fill box (1 px in from a 420 x 100 card), --bracket x --rule.
        const QRectF fill(1, 1, 418, 98);
        const QList<QRectF> bars =
            CutGeometry::brackets(fill, CutGeometry::BottomLeft | CutGeometry::BottomRight, 12, 2);
        QCOMPARE(bars, (QList<QRectF>{{1, 97, 12, 2}, {1, 87, 2, 12}, {407, 97, 12, 2}, {417, 87, 2, 12}}));
    }

    void bracketsOnAllCorners()
    {
        // The reticle's frame: arms 0.16 S, 1 px, S = 56.
        const qreal arm = 0.16 * 56;
        const QList<QRectF> bars = CutGeometry::brackets(QRectF(0, 0, 56, 56), 0xF, arm, 1);
        QCOMPARE(bars.size(), 8);
        QCOMPARE(bars.at(2), QRectF(56 - arm, 0, arm, 1)); // top-right, horizontal
        QCOMPARE(bars.at(3), QRectF(55, 0, 1, arm));       // top-right, vertical
    }

    void noBracketsRequested() { QVERIFY(CutGeometry::brackets(QRectF(0, 0, 10, 10), 0, 4, 1).isEmpty()); }
};

QTEST_GUILESS_MAIN(TestCutGeometry)
#include "tst_cutgeometry.moc"
