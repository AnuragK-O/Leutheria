#include "AgentClient.h"

#include <QJsonDocument>
#include <QJsonParseError>
#include <QLoggingCategory>

#include <algorithm>

Q_LOGGING_CATEGORY(lcClient, "leutheria.client", QtInfoMsg)

AgentClient::AgentClient(QUrl url, QObject *parent)
    : AgentClient(std::move(url), Backoff{}, parent)
{
}

AgentClient::AgentClient(QUrl url, Backoff backoff, QObject *parent)
    : QObject(parent)
    , m_url(std::move(url))
    , m_backoff(backoff)
    , m_retryDelay(backoff.initial)
{
    m_retryTimer.setSingleShot(true);
    connect(&m_retryTimer, &QTimer::timeout, this, &AgentClient::open);
    connect(&m_socket, &QWebSocket::connected, this, &AgentClient::onConnected);
    connect(&m_socket, &QWebSocket::stateChanged, this, &AgentClient::onStateChanged);
    connect(&m_socket, &QWebSocket::textMessageReceived, this, &AgentClient::onTextMessage);
}

AgentClient::~AgentClient()
{
    // Closing emits stateChanged; don't schedule a reconnect from a dying object.
    m_socket.disconnect(this);
    m_socket.abort();
}

void AgentClient::start()
{
    if (m_started)
        return;
    m_started = true;
    open();
}

void AgentClient::open()
{
    qCDebug(lcClient) << "connecting to" << m_url.toString();
    m_socket.open(m_url);
}

void AgentClient::onConnected()
{
    qCInfo(lcClient) << "connected to" << m_url.toString();
    m_retryDelay = m_backoff.initial;
    // Identify first, before anything else can be sent: the agent uses this to
    // keep the overlay from ever becoming the primary (reply-receiving) client.
    sendJson({{QStringLiteral("type"), QStringLiteral("hello")},
              {QStringLiteral("role"), QStringLiteral("overlay")}});
    setConnected(true);
}

// Driven off stateChanged rather than disconnected()/errorOccurred(): a
// refused connection and a dropped one both end in UnconnectedState, and
// this is the one signal guaranteed for both, exactly once per attempt.
void AgentClient::onStateChanged(QAbstractSocket::SocketState state)
{
    if (state != QAbstractSocket::UnconnectedState)
        return;
    if (m_connected)
        qCInfo(lcClient) << "connection lost:" << m_socket.errorString();
    setConnected(false);
    if (m_retryTimer.isActive())
        return;
    qCDebug(lcClient) << "retrying in" << m_retryDelay.count() << "ms";
    m_retryTimer.start(m_retryDelay);
    m_retryDelay = std::min(m_retryDelay * 2, m_backoff.max);
}

void AgentClient::onTextMessage(const QString &text)
{
    QJsonParseError error{};
    const QJsonDocument doc = QJsonDocument::fromJson(text.toUtf8(), &error);
    // Anything that isn't {"type": "<string>", ...} is dropped here, so the
    // model never has to defend against a non-object or a typeless payload.
    if (error.error != QJsonParseError::NoError || !doc.isObject()
        || !doc.object().value(QStringLiteral("type")).isString()) {
        qCDebug(lcClient) << "ignoring malformed message:" << text.left(120);
        return;
    }
    // The agent rejects anything an overlay connection isn't allowed to send
    // with {"type":"error"}; that's a bug on this side, so make it visible.
    if (doc.object().value(QStringLiteral("type")).toString() == QStringLiteral("error"))
        qCWarning(lcClient) << "agent error:" << doc.object().value(QStringLiteral("text")).toString();
    emit messageReceived(doc.object());
}

bool AgentClient::sendConfirm(const QString &id, bool approved)
{
    return sendJson({{QStringLiteral("type"), QStringLiteral("confirm")},
                     {QStringLiteral("id"), id},
                     {QStringLiteral("approved"), approved},
                     {QStringLiteral("remember"), QJsonValue::Null}});
}

bool AgentClient::sendJson(const QJsonObject &object)
{
    if (m_socket.state() != QAbstractSocket::ConnectedState) {
        qCWarning(lcClient) << "not connected; dropped" << object.value(QStringLiteral("type")).toString();
        return false;
    }
    m_socket.sendTextMessage(QString::fromUtf8(QJsonDocument(object).toJson(QJsonDocument::Compact)));
    return true;
}

void AgentClient::setConnected(bool connected)
{
    if (m_connected == connected)
        return;
    m_connected = connected;
    emit connectedChanged(connected);
}
