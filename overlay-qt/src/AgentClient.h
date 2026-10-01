#pragma once

#include <QJsonObject>
#include <QObject>
#include <QTimer>
#include <QUrl>
#include <QWebSocket>

#include <chrono>

// The overlay's one connection to the Python agent (or the mock agent).
//
// It is deliberately dumb: connect, identify as an overlay, turn text frames
// into JSON objects, and keep reconnecting forever with capped exponential
// backoff. What a message *means* is OverlayModel's business, so this class
// never looks past the "type" field.
//
// The overlay is a secondary client: the agent never makes it the "primary"
// connection (that's Electron's window), so nothing it doesn't answer can
// hang. That is why dropping a send while disconnected is acceptable here.
class AgentClient : public QObject
{
    Q_OBJECT
    Q_PROPERTY(bool connected READ isConnected NOTIFY connectedChanged)

public:
    struct Backoff
    {
        std::chrono::milliseconds initial{500};
        std::chrono::milliseconds max{8000};
    };

    explicit AgentClient(QUrl url, QObject *parent = nullptr);
    AgentClient(QUrl url, Backoff backoff, QObject *parent = nullptr);
    ~AgentClient() override;

    QUrl url() const { return m_url; }
    bool isConnected() const { return m_connected; }

    // The delay before the next reconnect attempt (exposed for tests).
    std::chrono::milliseconds nextRetryDelay() const { return m_retryDelay; }

public slots:
    void start();

    // {"type":"confirm","id":..,"approved":..,"remember":null}. The overlay
    // only offers a plain yes/no; the "remember" options stay in the main
    // window's full card. Returns false (and logs) if there's no connection.
    bool sendConfirm(const QString &id, bool approved);

signals:
    void connectedChanged(bool connected);
    // Every well-formed message: a JSON object with a string "type".
    void messageReceived(const QJsonObject &message);

private:
    void open();
    void onConnected();
    void onStateChanged(QAbstractSocket::SocketState state);
    void onTextMessage(const QString &text);
    bool sendJson(const QJsonObject &object);
    void setConnected(bool connected);

    QUrl m_url;
    Backoff m_backoff;
    std::chrono::milliseconds m_retryDelay;
    QWebSocket m_socket;
    QTimer m_retryTimer;
    bool m_connected = false;
    bool m_started = false;
};
