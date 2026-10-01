// AgentClient against a real, in-process QWebSocketServer: the handshake,
// reconnecting, the confirm payload, and what gets through to the model.

#include "AgentClient.h"

#include <QJsonDocument>
#include <QJsonObject>
#include <QSignalSpy>
#include <QTest>
#include <QWebSocket>
#include <QWebSocketServer>

#include <memory>

using namespace Qt::StringLiterals;
using namespace std::chrono_literals;

namespace {

// Fast retries so the reconnect tests take milliseconds, not seconds.
constexpr AgentClient::Backoff kFast{.initial = 20ms, .max = 160ms};
constexpr int kTimeout = 5000;

QJsonObject parse(const QString &text)
{
    return QJsonDocument::fromJson(text.toUtf8()).object();
}

// A server that records every connection and every frame it receives.
class FakeAgent : public QObject
{
public:
    explicit FakeAgent(quint16 port = 0)
        : server(u"fake-agent"_s, QWebSocketServer::NonSecureMode)
    {
        listen(port);
        QObject::connect(&server, &QWebSocketServer::newConnection, this, [this] {
            while (QWebSocket *socket = server.nextPendingConnection()) {
                socket->setParent(this);
                sockets.push_back(socket);
                QObject::connect(socket, &QWebSocket::textMessageReceived, this,
                                 [this](const QString &text) { received.push_back(parse(text)); });
            }
        });
    }

    bool listen(quint16 port) { return server.listen(QHostAddress::LocalHost, port); }
    QUrl url() const { return QUrl(u"ws://127.0.0.1:%1"_s.arg(server.serverPort())); }
    QWebSocket *latest() const { return sockets.empty() ? nullptr : sockets.back(); }

    QWebSocketServer server;
    std::vector<QWebSocket *> sockets;
    std::vector<QJsonObject> received;
};

} // namespace

class TestAgentClient : public QObject
{
    Q_OBJECT

private slots:
    void sendsHelloFirst()
    {
        FakeAgent agent;
        AgentClient client(agent.url(), kFast);
        QSignalSpy connected(&client, &AgentClient::connectedChanged);
        client.start();

        QTRY_COMPARE_WITH_TIMEOUT(agent.received.size(), size_t(1), kTimeout);
        const QJsonObject hello{{u"type"_s, u"hello"_s}, {u"role"_s, u"overlay"_s}};
        QCOMPARE(agent.received.front(), hello);
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);
        QCOMPARE(connected.count(), 1);
    }

    void reconnectsAfterADrop()
    {
        FakeAgent agent;
        AgentClient client(agent.url(), kFast);
        QSignalSpy connected(&client, &AgentClient::connectedChanged);
        client.start();
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);

        agent.latest()->close(); // the agent restarts, crashes, or drops us
        QTRY_VERIFY_WITH_TIMEOUT(!client.isConnected(), kTimeout);
        QTRY_COMPARE_WITH_TIMEOUT(agent.sockets.size(), size_t(2), kTimeout);
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);

        // Every new connection starts with its own hello.
        QTRY_COMPARE_WITH_TIMEOUT(agent.received.size(), size_t(2), kTimeout);
        QCOMPARE(agent.received.at(1).value(u"type"_s).toString(), u"hello"_s);
        QCOMPARE(connected.count(), 3); // up, down, up
    }

    // Electron may start the overlay before the agent is listening.
    void keepsRetryingUntilTheAgentAppears()
    {
        quint16 port = 0;
        {
            FakeAgent probe; // reserve a free port, then free it
            port = probe.server.serverPort();
        }
        AgentClient client(QUrl(u"ws://127.0.0.1:%1"_s.arg(port)), kFast);
        client.start();

        // Refused attempts back off, doubling up to the cap.
        QTRY_VERIFY_WITH_TIMEOUT(client.nextRetryDelay() == kFast.max, kTimeout);
        QVERIFY(!client.isConnected());

        FakeAgent agent(port);
        QVERIFY(agent.server.isListening());
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);
        QCOMPARE(client.nextRetryDelay(), kFast.initial); // reset on success
    }

    void confirmPayload()
    {
        FakeAgent agent;
        AgentClient client(agent.url(), kFast);
        client.start();
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);

        QVERIFY(client.sendConfirm(u"abc-123"_s, true));
        QVERIFY(client.sendConfirm(u"abc-124"_s, false));
        QTRY_COMPARE_WITH_TIMEOUT(agent.received.size(), size_t(3), kTimeout);

        const QJsonObject approve{{u"type"_s, u"confirm"_s},
                                  {u"id"_s, u"abc-123"_s},
                                  {u"approved"_s, true},
                                  {u"remember"_s, QJsonValue::Null}};
        QCOMPARE(agent.received.at(1), approve);
        QCOMPARE(agent.received.at(2).value(u"approved"_s), QJsonValue(false));
    }

    void confirmWhileDisconnectedIsDropped()
    {
        AgentClient client(QUrl(u"ws://127.0.0.1:9"_s), kFast); // never started
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"not connected; dropped"_s));
        QVERIFY(!client.sendConfirm(u"x"_s, true));
    }

    void onlyWellFormedMessagesGetThrough()
    {
        FakeAgent agent;
        AgentClient client(agent.url(), kFast);
        QSignalSpy messages(&client, &AgentClient::messageReceived);
        client.start();
        QTRY_VERIFY_WITH_TIMEOUT(agent.latest(), kTimeout);
        QWebSocket *socket = agent.latest();

        socket->sendTextMessage(u"not json"_s);
        socket->sendTextMessage(u"[1, 2, 3]"_s);
        socket->sendTextMessage(u"\"voice_state\""_s);
        socket->sendTextMessage(uR"({"state": "listening"})"_s); // no type
        socket->sendTextMessage(uR"({"type": 7})"_s);
        socket->sendBinaryMessage(QByteArray(R"({"type": "voice_state"})")); // binary frames aren't ours
        socket->sendTextMessage(uR"({"type": "voice_state", "state": "listening", "session": true})"_s);

        QTRY_COMPARE_WITH_TIMEOUT(messages.count(), 1, kTimeout);
        QTest::qWait(50); // nothing else trickles in after
        QCOMPARE(messages.count(), 1);
        const auto message = messages.first().first().value<QJsonObject>();
        QCOMPARE(message.value(u"state"_s).toString(), u"listening"_s);
    }
};

QTEST_GUILESS_MAIN(TestAgentClient)
#include "tst_agentclient.moc"
