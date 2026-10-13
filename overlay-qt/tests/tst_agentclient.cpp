// AgentClient against a real, in-process QWebSocketServer: the handshake,
// reconnecting, the confirm payload, and what gets through to the model.

#include "AgentClient.h"

#include <QJsonDocument>
#include <QJsonObject>
#include <QElapsedTimer>
#include <QHash>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QSignalSpy>
#include <QTcpServer>
#include <QTcpSocket>
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

// What the real agent does to a handshake it won't accept (ws_auth.py): a
// plain HTTP error instead of the upgrade. QWebSocketServer can't refuse on a
// header, so this is a bare TCP server that answers every request the same
// way and keeps what it was sent.
class RejectingAgent : public QObject
{
public:
    explicit RejectingAgent(int status)
        : status(status)
    {
        server.listen(QHostAddress::LocalHost, 0);
        QObject::connect(&server, &QTcpServer::newConnection, this, [this] {
            while (QTcpSocket *socket = server.nextPendingConnection()) {
                QObject::connect(socket, &QTcpSocket::readyRead, this, [this, socket] {
                    QByteArray &request = pending[socket];
                    request += socket->readAll();
                    if (!request.contains("\r\n\r\n"))
                        return;
                    requests.push_back(request);
                    attemptTimes.push_back(clock.elapsed());
                    pending.remove(socket);
                    const QByteArray reason = this->status == 401 ? "Unauthorized" : "Forbidden";
                    // The headers the real agent sends (ws_auth.py). They
                    // matter: Qt's error text for a 401 comes from how it
                    // handles the challenge, not from the status line.
                    const QByteArray challenge =
                        this->status == 401 ? "WWW-Authenticate: Bearer realm=\"leutheria\"\r\n" : "";
                    socket->write("HTTP/1.1 " + QByteArray::number(this->status) + " " + reason + "\r\n" + challenge
                                  + "Content-Type: text/plain; charset=utf-8\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
                    socket->disconnectFromHost();
                });
                QObject::connect(socket, &QTcpSocket::disconnected, socket, &QObject::deleteLater);
            }
        });
        clock.start();
    }

    quint16 port() const { return server.serverPort(); }
    QUrl url() const { return QUrl(u"ws://127.0.0.1:%1"_s.arg(server.serverPort())); }

    int status;
    QTcpServer server;
    QElapsedTimer clock;
    QHash<QTcpSocket *, QByteArray> pending;
    std::vector<QByteArray> requests;
    std::vector<qint64> attemptTimes;
};

const QByteArray kToken = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

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

    // The agent authenticates the opening handshake: the token goes in an
    // Authorization header, and there must be no Origin header at all (the
    // agent refuses any request that has one, as a browser's always does).
    void presentsTheTokenAndNoOrigin()
    {
        FakeAgent agent;
        AgentClient client(agent.url(), kFast);
        client.setAuthToken(kToken);
        client.start();
        QTRY_VERIFY_WITH_TIMEOUT(agent.latest(), kTimeout);

        const QNetworkRequest request = agent.latest()->request();
        QCOMPARE(request.rawHeader("Authorization"), QByteArray("Bearer ") + kToken);
        QVERIFY(!request.hasRawHeader("Origin"));
        QVERIFY(agent.latest()->origin().isEmpty());
        // Not smuggled into the URL either (URLs end up in logs).
        QVERIFY(!agent.latest()->requestUrl().toString().contains(QString::fromLatin1(kToken)));
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);
        QVERIFY(!client.authRejected());
    }

    // The same, read off the wire rather than through QWebSocketServer's
    // parsing, and on every retry.
    void rawHandshakeCarriesTheTokenOnEveryAttempt()
    {
        RejectingAgent agent(401);
        AgentClient client(agent.url(), kFast);
        client.setAuthToken(kToken);
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(u"refused the connection \\(HTTP 401\\)"_s));
        client.start();
        QTRY_VERIFY_WITH_TIMEOUT(agent.requests.size() >= 2, kTimeout);
        for (const QByteArray &request : agent.requests) {
            // Header names are case-insensitive, and Qt sends this one in
            // lower case; the value is exact.
            const qsizetype at = request.toLower().indexOf("\r\nauthorization: ");
            QVERIFY(at > 0);
            QVERIFY(request.mid(at + 17).startsWith("Bearer " + kToken + "\r\n"));
            QCOMPARE(request.toLower().count("\r\nauthorization:"), 1);
            QVERIFY(!request.toLower().contains("\r\norigin:"));
            QVERIFY(!request.left(request.indexOf("\r\n")).contains(kToken)); // not in the request line
        }
    }

    void withoutATokenNoAuthorizationHeaderIsSent()
    {
        FakeAgent agent;
        AgentClient client(agent.url(), kFast);
        client.start();
        QTRY_VERIFY_WITH_TIMEOUT(agent.latest(), kTimeout);
        QVERIFY(!agent.latest()->request().hasRawHeader("Authorization"));
        QVERIFY(!client.hasAuthToken());
    }

    void tokenComesFromTheEnvironmentAndIsRemovedFromIt()
    {
        qputenv("LEUTHERIA_TOKEN", kToken + "\n");
        QCOMPARE(AgentClient::takeTokenFromEnvironment(), kToken);
        QVERIFY(!qEnvironmentVariableIsSet("LEUTHERIA_TOKEN"));
        QVERIFY(AgentClient::takeTokenFromEnvironment().isEmpty());
    }

    // A missing or wrong token: the agent answers 401 before the upgrade.
    // That must be a clean, slow, reconnecting failure with one clear log
    // line -- not a crash, not a tight loop, not a line per attempt -- and
    // the client must still come up when an agent that accepts it appears.
    void refusedTokenIsACleanSlowRetry_data()
    {
        QTest::addColumn<QByteArray>("token");
        QTest::addColumn<int>("status");
        QTest::addColumn<QString>("logged");
        QTest::newRow("wrong token") << QByteArray(64, 'f') << 401
                                     << u"HTTP 401\\): LEUTHERIA_TOKEN is not the token that agent was started with"_s;
        QTest::newRow("no token") << QByteArray() << 401 << u"HTTP 401\\): no token -- LEUTHERIA_TOKEN is not set"_s;
        QTest::newRow("origin refused") << kToken << 403 << u"HTTP 403\\)"_s;
    }

    void refusedTokenIsACleanSlowRetry()
    {
        QFETCH(QByteArray, token);
        QFETCH(int, status);
        QFETCH(QString, logged);

        auto rejecting = std::make_unique<RejectingAgent>(status);
        const quint16 port = rejecting->port();
        AgentClient client(rejecting->url(), kFast);
        client.setAuthToken(token);
        QSignalSpy connected(&client, &AgentClient::connectedChanged);
        // Exactly one line for the whole run of refusals: the first is
        // expected, any further one fails the test.
        QTest::ignoreMessage(QtWarningMsg, QRegularExpression(logged));
        QTest::failOnWarning(QRegularExpression(u"refused the connection"_s));
        client.start();

        QTRY_VERIFY_WITH_TIMEOUT(rejecting->requests.size() >= 4, kTimeout);
        QVERIFY(!client.isConnected());
        QCOMPARE(connected.count(), 0);
        QVERIFY(client.authRejected());
        // Straight to the slowest retry from the first refusal, and it stays
        // there: every gap between attempts is about kFast.max, where a
        // client treating this as "not up yet" would start at kFast.initial
        // (which is what this did before the 401 text was matched properly).
        QCOMPARE(client.nextRetryDelay(), kFast.max);
        for (size_t i = 1; i < rejecting->attemptTimes.size(); ++i) {
            const qint64 gap = rejecting->attemptTimes[i] - rejecting->attemptTimes[i - 1];
            QVERIFY2(gap >= kFast.max.count() - 20, qPrintable(u"attempt %1 came %2 ms after the last"_s.arg(i).arg(gap)));
        }
        // The token never shows up in a log line.
        QVERIFY(!logged.contains(QString::fromLatin1(kToken)));

        // The agent is restarted with a token that matches: we recover.
        rejecting.reset();
        FakeAgent agent(port);
        QVERIFY(agent.server.isListening());
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);
        QVERIFY(!client.authRejected());
        QCOMPARE(client.nextRetryDelay(), kFast.initial);
    }

    void reconnectsAfterADrop()
    {
        FakeAgent agent;
        AgentClient client(agent.url(), kFast);
        QSignalSpy connected(&client, &AgentClient::connectedChanged);
        client.start();
        QTRY_VERIFY_WITH_TIMEOUT(client.isConnected(), kTimeout);

        agent.latest()->close(); // the agent restarts, crashes, or drops us
        // Asserted through the signal history, not by polling isConnected():
        // with kFast backoff the client can be down and back up between two
        // QTRY polls (50 ms apart), so "!isConnected()" was a race the test
        // could lose on a fast machine.
        QTRY_COMPARE_WITH_TIMEOUT(connected.count(), 3, kTimeout);
        QCOMPARE(connected.at(0).at(0).toBool(), true);
        QCOMPARE(connected.at(1).at(0).toBool(), false);
        QCOMPARE(connected.at(2).at(0).toBool(), true);
        QVERIFY(client.isConnected());
        QCOMPARE(agent.sockets.size(), size_t(2));

        // Every new connection starts with its own hello.
        QTRY_COMPARE_WITH_TIMEOUT(agent.received.size(), size_t(2), kTimeout);
        QCOMPARE(agent.received.at(1).value(u"type"_s).toString(), u"hello"_s);
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
