#pragma once

#include <QDir>
#include <QJsonObject>
#include <QObject>

class QQuickWindow;

// Debug only (--capture-dir): writes a PNG of the overlay for every message
// that changes what it shows, named like the Electron capture path's
// ("03-hearing.png", "06-confirm-card.png"), so the two overlays' frames can
// be compared side by side. Never used in normal runs.
class FrameCapture : public QObject
{
    Q_OBJECT

public:
    FrameCapture(QQuickWindow *window, const QString &dir, QObject *parent = nullptr);

public slots:
    void onMessage(const QJsonObject &message);

private:
    void captureSoon(const QString &label);

    QQuickWindow *m_window;
    QDir m_dir;
    int m_seq = 0;
};
