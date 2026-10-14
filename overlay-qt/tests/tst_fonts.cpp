// The bundled faces resolve to themselves -- no silent substitution
// (BUGS.md #32) -- and ligatures are off, as in the Electron overlay.

#include "Fonts.h"

#include <QFontInfo>
#include <QPainterPath>
#include <QRawFont>
#include <QTest>
#include <QTextLayout>

using namespace Qt::StringLiterals;

namespace {

// The glyph ids `text` shapes to with `font`.
QList<quint32> shape(const QFont &font, const QString &text)
{
    QTextLayout layout(text, font);
    layout.beginLayout();
    layout.createLine();
    layout.endLayout();
    QList<quint32> ids;
    for (const QGlyphRun &run : layout.glyphRuns())
        ids += run.glyphIndexes();
    return ids;
}

} // namespace

class TestFonts : public QObject
{
    Q_OBJECT

private slots:
    void initTestCase() { QVERIFY(Fonts::registerBundled()); }

    // Each (family, weight) the overlay asks for, and the face it must get.
    // QRawFont is what actually draws; QFontInfo is what Qt says it matched.
    void resolvesToTheBundledFace_data()
    {
        QTest::addColumn<bool>("isMono");
        QTest::addColumn<int>("weight");
        QTest::addColumn<QString>("family");
        QTest::addColumn<QString>("style");
        QTest::addColumn<int>("rawWeight");

        QTest::newRow("ui 400 (line, hint)") << false << 400 << Fonts::kUi << u"Regular"_s << 400;
        QTest::newRow("ui 500") << false << 500 << Fonts::kUiMedium << u"Regular"_s << 500;
        QTest::newRow("ui 600 (buttons)") << false << 600 << Fonts::kUiSemiBold << u"Regular"_s << 600;
        QTest::newRow("ui 700") << false << 700 << Fonts::kUi << u"Bold"_s << 700;
        QTest::newRow("mono 400 (code, outcome)") << true << 400 << Fonts::kMono << u"Regular"_s << 400;
        // The trap: a plain weight-500 request picks Bold (its OS/2 weight
        // is 558, Medium's 436). Asked for by style name, it's Medium.
        QTest::newRow("mono 500 (micro-labels)") << true << 500 << Fonts::kMono << u"Medium"_s << -1;
        QTest::newRow("mono 700") << true << 700 << Fonts::kMono << u"Bold"_s << -1;
    }

    void resolvesToTheBundledFace()
    {
        QFETCH(bool, isMono);
        QFETCH(int, weight);
        QFETCH(QString, family);
        QFETCH(QString, style);
        QFETCH(int, rawWeight);

        const QFont font = isMono ? Fonts::mono(weight, 13) : Fonts::ui(weight, 13);
        const QFontInfo info(font);
        QCOMPARE(info.family(), family);
        QCOMPARE(info.styleName(), style);
        QCOMPARE(info.fixedPitch(), isMono);

        const QRawFont raw = QRawFont::fromFont(font);
        QCOMPARE(raw.familyName(), family);
        QCOMPARE(raw.styleName(), style);
        if (rawWeight > 0)
            QCOMPARE(raw.weight(), rawWeight);
    }

    // A missing face would have fallen back silently to the same system font
    // for every request; distinct faces must draw distinct glyph outlines.
    void weightsAreDistinctFaces()
    {
        const QRawFont regular = QRawFont::fromFont(Fonts::mono(400, 13));
        const QRawFont medium = QRawFont::fromFont(Fonts::mono(500, 13));
        const QRawFont bold = QRawFont::fromFont(Fonts::mono(700, 13));
        const quint32 m = regular.glyphIndexesForString(u"m"_s).value(0);
        QVERIFY(regular.pathForGlyph(m) != medium.pathForGlyph(m));
        QVERIFY(medium.pathForGlyph(m) != bold.pathForGlyph(m));
    }

    // font-variant-ligatures: none. JetBrains Mono turns "->" into an arrow
    // through contextual alternates; with them off, each character keeps
    // the glyph it has on its own.
    void ligaturesAreOff_data()
    {
        QTest::addColumn<QString>("text");
        QTest::newRow("arrow") << u"->"_s;
        QTest::newRow("not equal") << u"!="_s;
        QTest::newRow("equality") << u"=="_s;
        QTest::newRow("fi") << u"fi"_s;
    }

    void ligaturesAreOff()
    {
        QFETCH(QString, text);
        for (const QFont &font : {Fonts::mono(400, 12), Fonts::ui(400, 13)}) {
            QList<quint32> alone;
            for (const QChar c : text)
                alone += shape(font, QString(c));
            QCOMPARE(shape(font, text), alone);
        }
    }

    // ...and that the check above can fail: with the features left on, the
    // mono face does substitute.
    void ligaturesExistWhenOn()
    {
        QFont font(Fonts::kMono);
        font.setPixelSize(12);
        QList<quint32> alone;
        for (const QChar c : u"->"_s)
            alone += shape(font, QString(c));
        QVERIFY(shape(font, u"->"_s) != alone);
    }

    void trackingIsCssEm()
    {
        const QFont font = Typeface::mono(500, 10, 0.12);
        QCOMPARE(font.letterSpacingType(), QFont::AbsoluteSpacing);
        // QFont keeps spacing in 26.6 fixed point: within 1/64 px.
        QVERIFY(qAbs(font.letterSpacing() - 1.2) <= 1.0 / 64);
    }
};

QTEST_MAIN(TestFonts)
#include "tst_fonts.moc"
