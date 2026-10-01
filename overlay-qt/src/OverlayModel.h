#pragma once

#include <QJsonObject>
#include <QObject>
#include <QString>
#include <QTimer>
#include <QtQml/qqmlregistration.h>

#include <optional>

// Everything the overlay shows, and every rule for when it changes.
//
// This is a port of the state handling in app/renderer/overlay/overlay.js,
// kept deliberately free of QML and of the socket: messages go in through
// handleMessage(), properties come out, and the tests drive it with plain
// QJsonObjects. QML only binds to the properties and calls answer().
//
// Lifecycle, in short:
//   session_started, or voice_state with session:true  -> shown
//   session_ended, voice_state with session:false,
//   or losing the agent connection                     -> "leaving", then
//                                                         hidden kLingerMs later
//   confirmation_required (only while in a session)    -> card up, clickable
//   answer() / confirmation_resolved / response        -> card settles and
//                                                         hides kSettleMs later
class OverlayModel : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    QML_UNCREATABLE("Created by main.cpp and handed to Main.qml")

    // Window-level visibility: true from session start until the fade-out
    // after it ends has had time to finish.
    Q_PROPERTY(bool visible READ isVisible NOTIFY visibleChanged)
    Q_PROPERTY(bool inSession READ inSession NOTIFY inSessionChanged)
    // One of the six visual states (never "off"/"idle": there's no overlay then).
    Q_PROPERTY(QString state READ state NOTIFY displayChanged)
    Q_PROPERTY(QString label READ label NOTIFY displayChanged)
    Q_PROPERTY(QString line READ line NOTIFY displayChanged)
    // Hint lines ("Catching that…") draw muted; quotes and replies don't.
    Q_PROPERTY(bool lineMuted READ lineMuted NOTIFY displayChanged)
    Q_PROPERTY(QString transcript READ transcript NOTIFY displayChanged)
    Q_PROPERTY(QString replyText READ replyText NOTIFY displayChanged)
    Q_PROPERTY(double level READ level NOTIFY levelChanged)

    // The compact confirmation card. `confirmationVisible` stays true for
    // kSettleMs after it's answered so the outcome can be read; only
    // `interactive` (card up *and* unanswered) makes the window clickable.
    Q_PROPERTY(bool confirmationVisible READ confirmationVisible NOTIFY confirmationChanged)
    Q_PROPERTY(bool confirmationAnswered READ confirmationAnswered NOTIFY confirmationChanged)
    Q_PROPERTY(QString confirmationId READ confirmationId NOTIFY confirmationChanged)
    Q_PROPERTY(QString confirmationTool READ confirmationTool NOTIFY confirmationChanged)
    Q_PROPERTY(QString confirmationCommand READ confirmationCommand NOTIFY confirmationChanged)
    Q_PROPERTY(bool confirmationApproved READ confirmationApproved NOTIFY confirmationChanged)
    Q_PROPERTY(QString confirmationOutcome READ confirmationOutcome NOTIFY confirmationChanged)
    Q_PROPERTY(bool interactive READ isInteractive NOTIFY interactiveChanged)

public:
    // How long the overlay stays mapped after a session ends (the QML fade is
    // shorter). Same value as main.js OVERLAY_LINGER_MS.
    static constexpr int kLingerMs = 650;
    // How long an answered confirmation card stays up showing the outcome.
    static constexpr int kSettleMs = 1100;
    // Longest reply shown in the island; the full text is in the main window.
    static constexpr int kReplyChars = 150;
    // Longest argument summary on the card.
    static constexpr int kArgsChars = 200;

    explicit OverlayModel(QObject *parent = nullptr);

    bool isVisible() const { return m_visible; }
    bool inSession() const { return m_inSession; }
    QString state() const { return m_state; }
    QString label() const { return m_label; }
    QString line() const { return m_line; }
    bool lineMuted() const { return m_lineMuted; }
    QString transcript() const { return m_transcript; }
    QString replyText() const { return m_reply; }
    double level() const { return m_level; }

    bool confirmationVisible() const { return m_card.has_value(); }
    bool confirmationAnswered() const { return m_card && m_card->answered; }
    QString confirmationId() const { return m_card ? m_card->id : QString(); }
    QString confirmationTool() const { return m_card ? m_card->tool : QString(); }
    QString confirmationCommand() const { return m_card ? m_card->command : QString(); }
    bool confirmationApproved() const { return m_card && m_card->approved; }
    QString confirmationOutcome() const { return m_card ? m_card->outcome : QString(); }
    bool isInteractive() const { return m_card && !m_card->answered; }

    // The same text shaping the Electron overlay uses; public for the tests.
    static QString shortReply(const QString &text);
    static QString formatArgs(const QJsonObject &args, int limit);

public slots:
    // Unknown types, wrong field types and out-of-order messages are all
    // ignored; nothing here trusts the wire.
    void handleMessage(const QJsonObject &message);
    // The agent went away: a session can't still be running as far as we know.
    void handleDisconnected();

    // Approve / Decline on the card. Emits confirmRequested and settles the
    // card at once; the agent's confirmation_resolved echo is then a no-op.
    Q_INVOKABLE void answer(bool approved);

signals:
    void visibleChanged(bool visible);
    void inSessionChanged(bool inSession);
    void displayChanged();
    void levelChanged(double level);
    void confirmationChanged();
    void interactiveChanged(bool interactive);
    // The user answered on the card; main.cpp forwards this to AgentClient.
    void confirmRequested(const QString &id, bool approved);

private:
    struct Card
    {
        QString id;
        QString tool;
        QString command;
        bool answered = false;
        bool approved = false;
        QString outcome;
    };

    void onVoiceState(const QJsonObject &message);
    void onAudioLevel(const QJsonObject &message);
    void onConfirmationRequired(const QJsonObject &message);
    void onConfirmationResolved(const QJsonObject &message);

    void startSession();
    void endSession(const QString &reason);
    void settleCard(bool approved, const QString &how);
    void hideCard();
    void setLevel(double level);
    void setVisible(bool visible);
    void setCard(std::optional<Card> card);
    void refresh(); // recompute label/line from state; emits displayChanged

    bool m_visible = false;
    bool m_inSession = false;
    QString m_state = QStringLiteral("listening");
    QString m_endedLabel; // set by endSession, overrides the state label
    QString m_transcript;
    QString m_reply;
    QString m_label;
    QString m_line;
    bool m_lineMuted = true;
    double m_level = 0.0;
    std::optional<Card> m_card;

    QTimer m_lingerTimer;
    QTimer m_settleTimer;
};
