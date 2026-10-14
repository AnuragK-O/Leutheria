#include "Fonts.h"

#include <QDirIterator>
#include <QFontDatabase>
#include <QLoggingCategory>

#include <algorithm>

Q_LOGGING_CATEGORY(lcFonts, "leutheria.fonts", QtInfoMsg)

using namespace Qt::StringLiterals;

namespace Fonts {

bool registerBundled()
{
    // WOFF2 loads directly (measured, Qt 6.11 / macOS: addApplicationFont
    // returns a valid id and the glyphs render), so there is no second,
    // unpacked copy of the fonts in overlay-qt/.
    bool ok = true;
    int count = 0;
    QDirIterator it(u":/fonts"_s, {u"*.woff2"_s}, QDir::Files);
    while (it.hasNext()) {
        const QString path = it.next();
        const int id = QFontDatabase::addApplicationFont(path);
        if (id < 0) {
            qCWarning(lcFonts) << "couldn't load bundled font" << path;
            ok = false;
            continue;
        }
        ++count;
        qCDebug(lcFonts) << path << "->" << QFontDatabase::applicationFontFamilies(id);
    }
    if (count == 0) {
        qCWarning(lcFonts) << "no bundled fonts in :/fonts; text falls back to system fonts";
        ok = false;
    }
    return ok;
}

namespace {

QFont withoutLigatures(QFont font)
{
    // CSS font-variant-ligatures: none turns off common, discretionary,
    // historical and contextual ligatures. JetBrains Mono's code ligatures
    // (-> != ==) live in calt.
    for (const QFont::Tag tag : {QFont::Tag("liga"), QFont::Tag("clig"), QFont::Tag("dlig"), QFont::Tag("hlig"),
                                 QFont::Tag("calt")})
        font.setFeature(tag, 0);
    return font;
}

} // namespace

QFont ui(int weight, qreal pixelSize)
{
    const QString family = weight >= 700 ? kUi : weight >= 600 ? kUiSemiBold : weight >= 500 ? kUiMedium : kUi;
    QFont font(family);
    font.setWeight(weight >= 700 ? QFont::Bold : QFont::Weight(std::max(weight, 400)));
    font.setPixelSize(qRound(pixelSize));
    return withoutLigatures(font);
}

QFont mono(int weight, qreal pixelSize)
{
    QFont font(kMono);
    if (weight >= 500 && weight < 700)
        font.setStyleName(kMonoMedium);
    else
        font.setWeight(weight >= 700 ? QFont::Bold : QFont::Normal);
    font.setPixelSize(qRound(pixelSize));
    return withoutLigatures(font);
}

} // namespace Fonts

QFont Typeface::ui(int weight, qreal pixelSize, qreal tracking)
{
    QFont font = Fonts::ui(weight, pixelSize);
    font.setLetterSpacing(QFont::AbsoluteSpacing, tracking * pixelSize);
    return font;
}

QFont Typeface::mono(int weight, qreal pixelSize, qreal tracking)
{
    QFont font = Fonts::mono(weight, pixelSize);
    font.setLetterSpacing(QFont::AbsoluteSpacing, tracking * pixelSize);
    return font;
}
