#pragma once

#include <QFont>
#include <QObject>
#include <QString>
#include <QtQml/qqmlregistration.h>

// The two bundled typefaces, the same files the Electron overlay loads
// (app/renderer/fonts/*.woff2, compiled in as Qt resources under :/fonts).
//
// Qt resolves a family by name and silently substitutes when the name
// doesn't match (BUGS.md #32), and these faces don't all share one family
// name: the webfont builds name Chakra Petch's 500 and 600 as their own
// families, and JetBrains Mono Medium is only reachable by style name (a
// weight-500 request picks Bold). So every face the overlay uses is named
// here, once, and tst_fonts checks each one resolves to itself.
namespace Fonts {

// Chakra Petch, for what a person reads (--font-ui).
inline const QString kUi = QStringLiteral("Chakra Petch");                // 400, 700
inline const QString kUiMedium = QStringLiteral("Chakra Petch Medium");   // 500
inline const QString kUiSemiBold = QStringLiteral("Chakra Petch SemiBold"); // 600
// JetBrains Mono, for what the machine said (--font-mono). One family;
// Medium (500) has to be asked for by style name.
inline const QString kMono = QStringLiteral("JetBrains Mono");
inline const QString kMonoMedium = QStringLiteral("Medium");

// Loads every bundled face into the application font database. Call once,
// after the QGuiApplication exists. Returns false, with a warning per file,
// if any face failed to load; the overlay still runs, in fallback fonts.
bool registerBundled();

// The face for a CSS weight (400/500/600/700) at a CSS pixel size, with
// ligatures off (font-variant-ligatures: none: `->` in a command must look
// like what will run). What QML builds through Theme.ui()/Theme.mono().
QFont ui(int weight, qreal pixelSize);
QFont mono(int weight, qreal pixelSize);

} // namespace Fonts

// QML's door to the above: `font: Typeface.mono(500, theme.fsMicro, theme.trackLabel)`.
// A singleton in Leutheria.Core, so the faces are named in C++ only.
class Typeface : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    QML_SINGLETON

public:
    using QObject::QObject;

    // tracking is CSS letter-spacing in em; Qt wants absolute pixels.
    Q_INVOKABLE static QFont ui(int weight, qreal pixelSize, qreal tracking = 0);
    Q_INVOKABLE static QFont mono(int weight, qreal pixelSize, qreal tracking = 0);
};
