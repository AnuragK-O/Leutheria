#include "AgentClient.h"

#include <QJsonDocument>
#include <QJsonParseError>
#include <QLoggingCategory>
#include <QNetworkRequest>
#include <QRegularExpression>

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
    connect(&m_socket, &QWebSocket::errorOccurred, this, &AgentClient::onError);
}

QByteArray AgentClient::takeTokenFromEnvironment()
{
    const QByteArray token = qgetenv("LEUTHERIA_TOKEN").trimmed();
    qunsetenv("LEUTHERIA_TOKEN");
    return token;
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
    // A QNetworkRequest rather than the bare URL, to carry the token. No
    // Origin header is added: QWebSocket only sends one when constructed
    // with a non-empty origin, m_socket isn't, and the agent refuses any
    // handshake that has one (tst_agentclient asserts both).
    QNetworkRequest request(m_url);
    if (!m_token.isEmpty())
        request.setRawHeader("Authorization", "Bearer " + m_token);
    m_socket.open(request);
}

void AgentClient::onConnected()
{
    qCInfo(lcClient) << "connected to" << m_url.toString();
    m_retryDelay = m_backoff.initial;
    m_rejectionLogged = false;
    m_authRejected = false;
    // Identify first, before anything else can be sent: the agent uses this to
    // keep the overlay from ever becoming the primary (reply-receiving) client.
    sendJson({{QStringLiteral("type"), QStringLiteral("hello")},
              {QStringLiteral("role"), QStringLiteral("overlay")}});
    setConnected(true);
}

// The agent answers a handshake it won't accept with a plain HTTP 401 (no or
// wrong token) or 403 (an Origin header) instead of the upgrade. That is not
// "the agent isn't up yet": say so, once per run of failures, and go
// straight to the slowest retry -- the token can't become right on its own,
// but the agent may be restarted with one that matches, so don't give up.
//
// QWebSocket has no status-code accessor, so this reads its error text,
// which was measured (Qt 6.11.2), not assumed:
//   403 -> "...processHandshake: Unhandled http status code: 403 (Forbidden)."
//   401 -> "...processHandshake: Unsupported WWW-Authenticate challenges
//          encountered." -- no status code at all: Qt tries to answer the
//          challenge itself and only knows Basic/Digest-style ones.
// Both arrive as errorOccurred *before* the UnconnectedState change, which
// is what lets the delay set here apply to the very next retry. Any other
// processHandshake failure (a different status, not a WebSocket server) is
// also "something answered and said no", and gets the same slow retry.
void AgentClient::onError(QAbstractSocket::SocketError)
{
    const QString text = m_socket.errorString();
    if (!text.contains(QLatin1String("processHandshake"))) {
        // Refused, timed out, dropped: the ordinary backoff applies.
        m_authRejected = false;
        return;
    }
    m_authRejected = true;
    m_retryDelay = m_backoff.max;
    if (m_rejectionLogged)
        return;
    m_rejectionLogged = true;
    static const QRegularExpression forbidden(QStringLiteral("\\b403\\b"));
    static const QRegularExpression unauthorized(QStringLiteral("\\b401\\b|WWW-Authenticate"));
    if (unauthorized.match(text).hasMatch()) {
        qCWarning(lcClient).noquote()
            << "the agent refused the connection (HTTP 401):"
            << (m_token.isEmpty() ? "no token -- LEUTHERIA_TOKEN is not set in this process's environment;"
                                  : "LEUTHERIA_TOKEN is not the token that agent was started with;")
            << "retrying every" << m_backoff.max.count() << "ms";
    } else if (forbidden.match(text).hasMatch()) {
        qCWarning(lcClient) << "the agent refused the connection (HTTP 403); retrying every"
                            << m_backoff.max.count() << "ms";
    } else {
        qCWarning(lcClient).noquote() << "the agent refused the connection (" + text + "); retrying every"
                                      << m_backoff.max.count() << "ms";
    }
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
