// LeutheriaOverlay: a second, native client of the agent's WebSocket protocol
// that draws the voice-session overlay. See overlay-qt/README.md.
//
// main() is only wiring: parse the CLI, build the three logic objects, hand
// two of them to QML, and connect model <-> socket <-> window.

#include "AgentClient.h"
#include "AssetManifest.h"
#include "FrameCapture.h"
#include "MacWindow.h"
#include "OverlayModel.h"

#include <QCommandLineParser>
#include <QCursor>
#include <QDir>
#include <QFontDatabase>
#include <QGuiApplication>
#include <QLoggingCategory>
#include <QMediaFormat>
#include <QMimeType>
#include <QQmlApplicationEngine>
#include <QQuickWindow>
#include <QScreen>
#include <QtQml/QQmlExtensionPlugin>

// Leutheria.Core lives in the static overlay_core library; a static QML
// plugin has to be pulled in explicitly or its types never register.
Q_IMPORT_QML_PLUGIN(Leutheria_CorePlugin)

Q_LOGGING_CATEGORY(lcMain, "leutheria.overlay")

using namespace Qt::StringLiterals;

namespace {

constexpr int kTopMargin = 12; // main.js OVERLAY_TOP_MARGIN

// The manifest lives in the Electron app's tree so both overlays read the
// same file. Walk up from the executable (inside the .app bundle inside
// overlay-qt/build/) until the repo root is found.
QString defaultAssetsDir()
{
    const QString relative = u"app/renderer/overlay/assets"_s;
    QDir dir(QCoreApplication::applicationDirPath());
    do {
        if (dir.exists(relative + u"/manifest.json"_s))
            return dir.filePath(relative);
    } while (dir.cdUp());
    return {};
}

// Whether Qt Multimedia's backend on this machine can decode a container.
// On macOS the backend is AVFoundation, which has no WebM support -- this is
// what turns a designer's listening.webm into a logged placeholder rather
// than a blank slot.
bool canPlayVideo(const QString &suffix)
{
    static const QStringList suffixes = [] {
        QStringList out;
        QMediaFormat format; // supportedFileFormats() is non-const
        for (const auto container : format.supportedFileFormats(QMediaFormat::Decode))
            out << QMediaFormat(container).mimeType().suffixes();
        qCInfo(lcMain) << "video containers this media backend decodes:" << out;
        return out;
    }();
    return suffixes.contains(suffix);
}

// Top-center of the display the cursor is on, like main.js positionOverlay().
void placeTopCenter(QWindow *window)
{
    QScreen *screen = QGuiApplication::screenAt(QCursor::pos());
    if (!screen)
        screen = QGuiApplication::primaryScreen();
    const QRect area = screen->availableGeometry(); // below the menu bar
    window->setScreen(screen);
    window->setPosition(area.x() + (area.width() - window->width()) / 2, area.y() + kTopMargin);
}

} // namespace

int main(int argc, char *argv[])
{
    QGuiApplication app(argc, argv);
    QGuiApplication::setApplicationName(u"LeutheriaOverlay"_s);
    QGuiApplication::setApplicationVersion(u"0.1.0"_s);
    MacWindow::neverActivate();

    QCommandLineParser cli;
    cli.setApplicationDescription(u"Leutheria voice-session overlay"_s);
    cli.addHelpOption();
    cli.addVersionOption();
    const QCommandLineOption urlOption(u"url"_s, u"Agent WebSocket URL."_s, u"url"_s,
                                       u"ws://127.0.0.1:8765"_s);
    const QCommandLineOption assetsOption(u"assets"_s, u"Overlay asset folder (holds manifest.json)."_s,
                                          u"dir"_s);
    const QCommandLineOption captureOption(u"capture-dir"_s,
                                           u"Debug: save a PNG of the overlay on every state change."_s,
                                           u"dir"_s);
    const QCommandLineOption themeOption(u"theme"_s, u"Force light or dark (default: follow macOS)."_s,
                                         u"light|dark"_s);
    cli.addOptions({urlOption, assetsOption, captureOption, themeOption});
    cli.process(app);

    const QUrl url(cli.value(urlOption));
    if (!url.isValid() || (url.scheme() != "ws"_L1 && url.scheme() != "wss"_L1)) {
        qCCritical(lcMain) << "--url must be a ws:// or wss:// URL, got" << cli.value(urlOption);
        return 2;
    }
    const QString theme = cli.value(themeOption);
    if (!theme.isEmpty() && theme != "light"_L1 && theme != "dark"_L1) {
        qCCritical(lcMain) << "--theme must be light or dark, got" << theme;
        return 2;
    }

    AssetManifest manifest;
    manifest.setVideoSupport(canPlayVideo);
    const QString assetsDir = cli.isSet(assetsOption) ? cli.value(assetsOption) : defaultAssetsDir();
    if (assetsDir.isEmpty())
        qCWarning(lcMain) << "couldn't find app/renderer/overlay/assets; pass --assets. Using placeholders.";
    else
        qCInfo(lcMain).noquote() << "assets:" << QDir(assetsDir).absolutePath();
    manifest.load(assetsDir);

    OverlayModel model;
    AgentClient client(url);
    QObject::connect(&client, &AgentClient::messageReceived, &model, &OverlayModel::handleMessage);
    QObject::connect(&client, &AgentClient::connectedChanged, &model, [&model](bool connected) {
        if (!connected)
            model.handleDisconnected();
    });
    QObject::connect(&model, &OverlayModel::confirmRequested, &client, &AgentClient::sendConfirm);

    QQmlApplicationEngine engine;
    engine.setInitialProperties({
        {u"overlay"_s, QVariant::fromValue(&model)},
        {u"manifest"_s, QVariant::fromValue(&manifest)},
        {u"forcedTheme"_s, theme},
        {u"monoFamily"_s, QFontDatabase::systemFont(QFontDatabase::FixedFont).family()},
    });
    engine.loadFromModule("Leutheria.Overlay", "Main");
    if (engine.rootObjects().isEmpty())
        return 1;
    auto *window = qobject_cast<QQuickWindow *>(engine.rootObjects().constFirst());
    Q_ASSERT(window);

    window->create();
    MacWindow::configure(window);

    QObject::connect(&model, &OverlayModel::visibleChanged, window, [window](bool visible) {
        if (visible) {
            // Only on the hidden -> shown edge, so a session never jumps
            // displays because the mouse moved.
            placeTopCenter(window);
            MacWindow::showInactive(window);
            qCInfo(lcMain) << "[overlay] shown at" << window->geometry();
        } else {
            MacWindow::setInteractive(window, false);
            MacWindow::hide(window);
            qCInfo(lcMain) << "[overlay] hidden";
        }
    });
    QObject::connect(&model, &OverlayModel::interactiveChanged, window,
                     [window](bool interactive) { MacWindow::setInteractive(window, interactive); });

    if (cli.isSet(captureOption)) {
        auto *capture = new FrameCapture(window, cli.value(captureOption), &app);
        QObject::connect(&client, &AgentClient::messageReceived, capture, &FrameCapture::onMessage);
    }

    client.start();
    return app.exec();
}
