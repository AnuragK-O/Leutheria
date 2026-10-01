// AssetManifest: the same validation and placeholder fallbacks as main.js
// loadOverlayManifest(), against throwaway asset folders.

#include "AssetManifest.h"

#include <QDir>
#include <QFile>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QTest>

using namespace Qt::StringLiterals;
using Type = AssetManifest::Type;

namespace {

void write(const QString &path, const QByteArray &contents)
{
    QFile file(path);
    QVERIFY2(file.open(QIODevice::WriteOnly), qPrintable(path));
    file.write(contents);
}

} // namespace

class TestAssetManifest : public QObject
{
    Q_OBJECT

private:
    QTemporaryDir m_root; // holds assets/ plus a sibling outside it
    QString m_assets;

    void writeManifest(const QByteArray &json) { write(m_assets + u"/manifest.json"_s, json); }

    void expectAllPlaceholders(const AssetManifest &manifest)
    {
        for (const QString &state : AssetManifest::states())
            QCOMPARE(manifest.entry(state).type, Type::Css);
    }

private slots:
    void init()
    {
        QVERIFY(m_root.isValid());
        m_assets = m_root.filePath(u"assets"_s);
        QDir(m_assets).removeRecursively();
        QVERIFY(QDir().mkpath(m_assets));
        write(m_assets + u"/listening.png"_s, "png");
        write(m_assets + u"/hearing.gif"_s, "gif");
        write(m_assets + u"/thinking.webm"_s, "webm");
        write(m_assets + u"/speaking.mov"_s, "mov");
        write(m_root.filePath(u"secret.png"_s), "outside");
    }

    void defaultsBeforeLoad()
    {
        AssetManifest manifest;
        expectAllPlaceholders(manifest);
        QCOMPARE(manifest.slotSize(), 56);
        QCOMPARE(manifest.entries().size(), AssetManifest::states().size());
    }

    // The manifest.json checked into app/renderer/overlay/assets.
    void allCssIsQuiet()
    {
        writeManifest(R"({"size": 56, "states": {"listening": {"type": "css"}, "thinking": {"type": "css"}}})");
        AssetManifest manifest;
        manifest.load(m_assets);
        expectAllPlaceholders(manifest);
        QVERIFY(manifest.problems().isEmpty());
    }

    void validEntriesLoad()
    {
        writeManifest(R"({"size": 64, "states": {
            "listening": {"type": "image", "src": "listening.png", "react": "scale"},
            "hearing":   {"type": "image", "src": "hearing.gif"},
            "thinking":  {"type": "video", "src": "thinking.webm", "loop": false, "react": "glow"}
        }})");
        AssetManifest manifest;
        QSignalSpy changed(&manifest, &AssetManifest::changed);
        manifest.load(m_assets);
        QCOMPARE(changed.count(), 1);
        QVERIFY2(manifest.problems().isEmpty(), qPrintable(manifest.problems().join(u'\n')));
        QCOMPARE(manifest.slotSize(), 64);

        const auto listening = manifest.entry(u"listening"_s);
        QCOMPARE(listening.type, Type::Image);
        QCOMPARE(listening.file, QFileInfo(m_assets + u"/listening.png"_s).canonicalFilePath());
        QCOMPARE(listening.react, u"scale"_s);

        const auto thinking = manifest.entry(u"thinking"_s);
        QCOMPARE(thinking.type, Type::Video);
        QCOMPARE(thinking.loop, false);
        QCOMPARE(thinking.react, u"glow"_s);

        QCOMPARE(manifest.entry(u"hearing"_s).loop, true);   // default
        QCOMPARE(manifest.entry(u"hearing"_s).react, u"none"_s);
        QCOMPARE(manifest.entry(u"speaking"_s).type, Type::Css); // absent -> placeholder

        // What QML sees.
        const QVariantMap qml = manifest.entries().value(u"listening"_s).toMap();
        QCOMPARE(qml.value(u"type"_s).toString(), u"image"_s);
        QCOMPARE(qml.value(u"source"_s).toUrl(), QUrl::fromLocalFile(listening.file));
    }

    void badEntriesFallBackOneByOne_data()
    {
        QTest::addColumn<QByteArray>("entry");
        QTest::addColumn<QString>("problem");
        QTest::newRow("unknown type") << QByteArray(R"({"type": "gif", "src": "hearing.gif"})") << u"unknown type"_s;
        QTest::newRow("typo'd type") << QByteArray(R"({"type": "Image", "src": "listening.png"})") << u"unknown type"_s;
        QTest::newRow("no type") << QByteArray(R"({"src": "listening.png"})") << u"unknown type"_s;
        QTest::newRow("not an object") << QByteArray(R"("listening.png")") << u"unknown type"_s;
        QTest::newRow("no src") << QByteArray(R"({"type": "image"})") << u"no src"_s;
        QTest::newRow("missing file") << QByteArray(R"({"type": "image", "src": "nope.png"})") << u"not found"_s;
        QTest::newRow("a directory") << QByteArray(R"({"type": "image", "src": "."})") << u"not found"_s;
    }

    void badEntriesFallBackOneByOne()
    {
        QFETCH(QByteArray, entry);
        QFETCH(QString, problem);
        writeManifest(R"({"states": {"hearing": )" + entry
                      + R"(, "listening": {"type": "image", "src": "listening.png"}}})");
        AssetManifest manifest;
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"hearing: .*"_s + problem));
        manifest.load(m_assets);
        QCOMPARE(manifest.entry(u"hearing"_s).type, Type::Css);
        // A bad entry costs only its own state.
        QCOMPARE(manifest.entry(u"listening"_s).type, Type::Image);
        QCOMPARE(manifest.problems().size(), 1);
    }

    void pathEscapeIsRejected_data()
    {
        QTest::addColumn<QString>("src");
        QTest::newRow("dot-dot") << u"../secret.png"_s;
        QTest::newRow("nested dot-dot") << u"sub/../../secret.png"_s;
        QTest::newRow("absolute") << m_root.filePath(u"secret.png"_s);
    }

    void pathEscapeIsRejected()
    {
        QFETCH(QString, src);
        writeManifest(u"{\"states\": {\"listening\": {\"type\": \"image\", \"src\": \"%1\"}}}"_s.arg(src).toUtf8());
        AssetManifest manifest;
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"listening: .* not found in assets/"_s));
        manifest.load(m_assets);
        QCOMPARE(manifest.entry(u"listening"_s).type, Type::Css);
    }

    // Stricter than main.js: a symlink inside assets/ pointing out of it.
    void symlinkEscapeIsRejected()
    {
        QVERIFY(QFile::link(m_root.filePath(u"secret.png"_s), m_assets + u"/link.png"_s));
        writeManifest(R"({"states": {"listening": {"type": "image", "src": "link.png"}}})");
        AssetManifest manifest;
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"listening: link.png not found"_s));
        manifest.load(m_assets);
        QCOMPARE(manifest.entry(u"listening"_s).type, Type::Css);
    }

    void missingManifestMeansPlaceholders()
    {
        AssetManifest manifest;
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"manifest unreadable"_s));
        manifest.load(m_assets); // no manifest.json written
        expectAllPlaceholders(manifest);
        QCOMPARE(manifest.slotSize(), 56);
    }

    void missingFolderMeansPlaceholders()
    {
        AssetManifest manifest;
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"manifest unreadable"_s));
        manifest.load(m_root.filePath(u"does-not-exist"_s));
        expectAllPlaceholders(manifest);
    }

    void brokenJsonMeansPlaceholders_data()
    {
        QTest::addColumn<QByteArray>("json");
        QTest::newRow("syntax") << QByteArray(R"({"states": {"listening": )");
        QTest::newRow("array") << QByteArray(R"([1, 2])");
        QTest::newRow("empty") << QByteArray("");
    }

    void brokenJsonMeansPlaceholders()
    {
        QFETCH(QByteArray, json);
        writeManifest(json);
        AssetManifest manifest;
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"manifest unreadable"_s));
        manifest.load(m_assets);
        expectAllPlaceholders(manifest);
    }

    void reloadReplacesPreviousEntries()
    {
        writeManifest(R"({"size": 80, "states": {"listening": {"type": "image", "src": "listening.png"}}})");
        AssetManifest manifest;
        manifest.load(m_assets);
        QCOMPARE(manifest.entry(u"listening"_s).type, Type::Image);
        writeManifest(R"({"states": {}})");
        manifest.load(m_assets);
        QCOMPARE(manifest.entry(u"listening"_s).type, Type::Css);
        QCOMPARE(manifest.slotSize(), 56);
    }

    void slotSizeIsClamped_data()
    {
        QTest::addColumn<QByteArray>("size");
        QTest::addColumn<int>("expected");
        QTest::newRow("small") << QByteArray("4") << 32;
        QTest::newRow("large") << QByteArray("900") << 120;
        QTest::newRow("fraction") << QByteArray("60.4") << 60;
        QTest::newRow("zero") << QByteArray("0") << 56;
        QTest::newRow("string") << QByteArray("\"big\"") << 56;
        QTest::newRow("null") << QByteArray("null") << 56;
    }

    void slotSizeIsClamped()
    {
        QFETCH(QByteArray, size);
        QFETCH(int, expected);
        writeManifest("{\"size\": " + size + ", \"states\": {}}");
        AssetManifest manifest;
        manifest.load(m_assets);
        QCOMPARE(manifest.slotSize(), expected);
    }

    // Qt-only rule: a video the media backend can't decode (WebM on macOS's
    // AVFoundation backend) is a placeholder with a reason, not a blank slot.
    void undecodableVideoFallsBack()
    {
        writeManifest(R"({"states": {
            "thinking": {"type": "video", "src": "thinking.webm"},
            "speaking": {"type": "video", "src": "speaking.mov"}
        }})");
        AssetManifest manifest;
        manifest.setVideoSupport([](const QString &suffix) { return suffix == u"mov"_s; });
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"thinking: .*can't play .webm"_s));
        manifest.load(m_assets);
        QCOMPARE(manifest.entry(u"thinking"_s).type, Type::Css);
        QCOMPARE(manifest.entry(u"speaking"_s).type, Type::Video);
    }

    void markBrokenSwapsToPlaceholder()
    {
        writeManifest(R"({"states": {"listening": {"type": "image", "src": "listening.png"}}})");
        AssetManifest manifest;
        manifest.load(m_assets);
        QSignalSpy changed(&manifest, &AssetManifest::changed);

        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"listening: asset failed to load"_s));
        manifest.markBroken(u"listening"_s, u"decode error"_s);
        QCOMPARE(manifest.entry(u"listening"_s).type, Type::Css);
        QCOMPARE(changed.count(), 1);

        // Already a placeholder, or not a state at all: nothing to do.
        manifest.markBroken(u"listening"_s, u"again"_s);
        manifest.markBroken(u"bogus"_s, u"x"_s);
        QCOMPARE(changed.count(), 1);
    }
};

QTEST_GUILESS_MAIN(TestAssetManifest)
#include "tst_assetmanifest.moc"
