#include "FrameCapture.h"

#include <QImage>
#include <QLoggingCategory>
#include <QQuickWindow>
#include <QTimer>

Q_LOGGING_CATEGORY(lcCapture, "leutheria.capture")

using namespace Qt::StringLiterals;

namespace {
// Let the state's transitions (fade, drop-in, colour change) settle first;
// the same delay main.js captureWindows() uses.
constexpr int kSettleDelayMs = 500;
} // namespace

FrameCapture::FrameCapture(QQuickWindow *window, const QString &dir, QObject *parent)
    : QObject(parent)
    , m_window(window)
    , m_dir(dir)
{
    if (!m_dir.mkpath(u"."_s))
        qCWarning(lcCapture) << "can't create capture dir" << dir;
}

void FrameCapture::onMessage(const QJsonObject &message)
{
    const QString type = message.value("type"_L1).toString();
    if (type == "voice_state"_L1)
        captureSoon(message.value("state"_L1).toString());
    else if (type == "confirmation_required"_L1)
        captureSoon(u"confirm-card"_s);
    else if (type == "confirmation_resolved"_L1)
        captureSoon(u"confirm-resolved"_s);
    else if (type == "speaking"_L1 && message.value("state"_L1).toString() == "started"_L1)
        captureSoon(u"speaking-text"_s);
    else if (type == "session_ended"_L1)
        captureSoon(u"ended"_s);
}

void FrameCapture::captureSoon(const QString &label)
{
    const QString name = u"%1-%2.png"_s.arg(++m_seq, 2, 10, QChar(u'0')).arg(label);
    QTimer::singleShot(kSettleDelayMs, this, [this, name] {
        if (!m_window->isVisible())
            return; // nothing on screen: idle frames would just be empty PNGs
        const QImage frame = m_window->grabWindow();
        const QString path = m_dir.filePath(name);
        if (frame.isNull() || !frame.save(path))
            qCWarning(lcCapture) << "capture failed:" << path;
        else
            qCInfo(lcCapture).noquote() << "[capture]" << path;
    });
}
