#include "OverlayModel.h"

#include <QJsonArray>
#include <QJsonDocument>
#include <QRegularExpression>

#include <algorithm>
#include <array>
#include <cmath>

namespace {

using namespace Qt::StringLiterals;

struct StateText
{
    QLatin1StringView state;
    QLatin1StringView label;
};

constexpr std::array kStates{
    StateText{"listening"_L1, "Listening"_L1},
    StateText{"hearing"_L1, "Hearing you"_L1},
    StateText{"transcribing"_L1, "Transcribing"_L1},
    StateText{"thinking"_L1, "Thinking"_L1},
    StateText{"speaking"_L1, "Speaking"_L1},
    StateText{"awaiting_confirmation"_L1, "Needs your OK"_L1},
};

bool isVisualState(const QString &state)
{
    return std::ranges::any_of(kStates, [&](const StateText &s) { return s.state == state; });
}

QString labelFor(const QString &state)
{
    const auto it = std::ranges::find_if(kStates, [&](const StateText &s) { return s.state == state; });
    return it != kStates.end() ? QString(it->label) : u"Listening"_s;
}

// The mic level only means something while the mic is live.
bool micIsLive(const QString &state)
{
    return state == "listening"_L1 || state == "hearing"_L1 || state == "awaiting_confirmation"_L1;
}

QString endedLabel(const QString &reason)
{
    if (reason == "dismissed"_L1)
        return u"Talk soon"_s;
    if (reason == "error"_L1)
        return u"Something went wrong"_s;
    return u"Stopped listening"_s; // timeout, manual, and anything unexpected
}

// The agent already smooths RMS, but only ticks at ~12-15 Hz. A fast attack
// and a slower release make the orb follow the voice without flickering to
// zero between syllables; QML interpolates between the ticks.
constexpr double kAttack = 0.6;
constexpr double kRelease = 0.25;

} // namespace

OverlayModel::OverlayModel(QObject *parent)
    : QObject(parent)
{
    m_lingerTimer.setSingleShot(true);
    m_lingerTimer.setInterval(kLingerMs);
    connect(&m_lingerTimer, &QTimer::timeout, this, [this] {
        // A session that restarted during the linger keeps the window up.
        if (!m_inSession)
            setVisible(false);
    });

    m_settleTimer.setSingleShot(true);
    m_settleTimer.setInterval(kSettleMs);
    connect(&m_settleTimer, &QTimer::timeout, this, [this] { setCard(std::nullopt); });

    refresh();
}

// --- inbound messages ------------------------------------------------------

void OverlayModel::handleMessage(const QJsonObject &message)
{
    const QString type = message.value("type"_L1).toString();

    if (type == "audio_level"_L1) {
        onAudioLevel(message);
    } else if (type == "session_started"_L1) {
        startSession();
    } else if (type == "session_ended"_L1) {
        if (m_inSession)
            endSession(message.value("reason"_L1).toString());
    } else if (type == "voice_state"_L1) {
        onVoiceState(message);
    } else if (type == "transcript"_L1) {
        // Push-to-talk transcripts belong to the main window's chat, not here.
        if (m_inSession && message.value("origin"_L1).toString() == "voice_session"_L1) {
            m_transcript = message.value("text"_L1).toString();
            refresh();
        }
    } else if (type == "speaking"_L1) {
        const QString text = message.value("text"_L1).toString();
        if (message.value("state"_L1).toString() == "started"_L1 && !text.isEmpty()) {
            m_reply = text;
            refresh();
        }
    } else if (type == "response"_L1) {
        // The command this session was running has finished, so whatever it
        // was waiting on is over -- answered, declined, or timed out.
        if (isInteractive())
            hideCard();
    } else if (type == "confirmation_required"_L1) {
        onConfirmationRequired(message);
    } else if (type == "confirmation_resolved"_L1) {
        onConfirmationResolved(message);
    }
    // Anything else (control_result, skill_proposed, future types) isn't ours.
}

void OverlayModel::onVoiceState(const QJsonObject &message)
{
    const QJsonValue session = message.value("session"_L1);
    if (!session.isBool())
        return;

    if (!session.toBool()) {
        // session_ended normally arrives first; this covers a missed one.
        if (m_inSession)
            endSession(u"manual"_s);
        return;
    }

    if (!m_inSession)
        startSession();

    const QString state = message.value("state"_L1).toString();
    if (!isVisualState(state))
        return;
    m_state = state;
    if (!micIsLive(state))
        setLevel(0.0);
    // A new utterance starts: the previous reply is stale.
    if (state == "hearing"_L1)
        m_reply.clear();
    refresh();
}

void OverlayModel::onAudioLevel(const QJsonObject &message)
{
    // A stray tick outside a live mic (e.g. racing a state change) would
    // light up a thinking orb; drop it.
    if (!m_inSession || !micIsLive(m_state))
        return;
    const QJsonValue value = message.value("level"_L1);
    if (!value.isDouble())
        return;
    if (!std::isfinite(value.toDouble()))
        return;
    const double target = std::clamp(value.toDouble(), 0.0, 1.0);
    const double k = target > m_level ? kAttack : kRelease;
    setLevel(m_level + (target - m_level) * k);
}

void OverlayModel::onConfirmationRequired(const QJsonObject &message)
{
    // Outside a session the main window's full card is the only one: the
    // overlay isn't even on screen.
    if (!m_inSession)
        return;
    const QString id = message.value("id"_L1).toString();
    const QString tool = message.value("tool"_L1).toString();
    if (id.isEmpty() || tool.isEmpty())
        return;

    m_settleTimer.stop();
    const QString args = formatArgs(message.value("args"_L1).toObject(), kArgsChars);
    setCard(Card{.id = id, .tool = tool, .command = u"%1(%2)"_s.arg(tool, args)});
    refresh(); // the awaiting_confirmation line names the tool
}

void OverlayModel::onConfirmationResolved(const QJsonObject &message)
{
    if (!isInteractive() || message.value("id"_L1).toString() != m_card->id)
        return;
    const QJsonValue approved = message.value("approved"_L1);
    if (!approved.isBool())
        return;
    // Our own answer never gets here (the card settled in answer()), so
    // this was decided somewhere else; same wording as overlay.js.
    // by:"overlay" on a card still open here means a second overlay answered.
    const QString by = message.value("by"_L1).toString();
    const QString how = by == "voice"_L1     ? u" by voice"_s
                        : by == "overlay"_L1 ? u" on the other overlay"_s
                                             : u" in the main window"_s;
    settleCard(approved.toBool(), how);
}

void OverlayModel::handleDisconnected()
{
    if (m_inSession)
        endSession(u"error"_s);
}

void OverlayModel::answer(bool approved)
{
    if (!isInteractive())
        return;
    const QString id = m_card->id;
    settleCard(approved, QString());
    emit confirmRequested(id, approved);
}

// --- transitions -------------------------------------------------------------

void OverlayModel::startSession()
{
    m_lingerTimer.stop();
    m_endedLabel.clear();
    m_transcript.clear();
    m_reply.clear();
    m_state = u"listening"_s;
    hideCard();
    setLevel(0.0);
    if (!m_inSession) {
        m_inSession = true;
        emit inSessionChanged(true);
    }
    setVisible(true);
    refresh();
}

void OverlayModel::endSession(const QString &reason)
{
    m_inSession = false;
    emit inSessionChanged(false);
    hideCard();
    setLevel(0.0);
    m_endedLabel = endedLabel(reason);
    refresh();
    if (m_visible)
        m_lingerTimer.start();
}

void OverlayModel::settleCard(bool approved, const QString &how)
{
    Card card = *m_card;
    card.answered = true;
    card.approved = approved;
    card.outcome = (approved ? u"Approved"_s : u"Declined"_s) + how;
    setCard(std::move(card));
    m_settleTimer.start();
}

void OverlayModel::hideCard()
{
    m_settleTimer.stop();
    setCard(std::nullopt);
}

void OverlayModel::setCard(std::optional<Card> card)
{
    const bool wasInteractive = isInteractive();
    if (!m_card && !card)
        return;
    m_card = std::move(card);
    emit confirmationChanged();
    if (wasInteractive != isInteractive())
        emit interactiveChanged(isInteractive());
    // The awaiting_confirmation line depends on whether a card is pending.
    refresh();
}

void OverlayModel::setLevel(double level)
{
    if (qFuzzyCompare(1.0 + m_level, 1.0 + level))
        return;
    m_level = level;
    emit levelChanged(level);
}

void OverlayModel::setVisible(bool visible)
{
    if (m_visible == visible)
        return;
    m_visible = visible;
    emit visibleChanged(visible);
}

void OverlayModel::refresh()
{
    QString label = m_endedLabel.isEmpty() ? labelFor(m_state) : m_endedLabel;
    QString line;
    bool muted = true;

    if (m_state == "listening"_L1) {
        line = m_reply.isEmpty() ? u"Go ahead. Say “thanks” when you're done."_s : shortReply(m_reply);
    } else if (m_state == "hearing"_L1) {
        line = u"…"_s;
    } else if (m_state == "transcribing"_L1) {
        line = u"Catching that…"_s;
    } else if (m_state == "thinking"_L1) {
        muted = m_transcript.isEmpty();
        line = muted ? u"Working on it…"_s : u"“%1”"_s.arg(m_transcript);
    } else if (m_state == "speaking"_L1) {
        muted = m_reply.isEmpty();
        line = muted ? u"…"_s : shortReply(m_reply);
    } else if (m_state == "awaiting_confirmation"_L1) {
        muted = false;
        line = isInteractive() ? u"Okay to run %1?"_s.arg(m_card->tool) : u"Waiting for your answer"_s;
    }

    if (label == m_label && line == m_line && muted == m_lineMuted)
        return;
    m_label = std::move(label);
    m_line = std::move(line);
    m_lineMuted = muted;
    emit displayChanged();
}

// --- text shaping --------------------------------------------------------------

QString OverlayModel::shortReply(const QString &text)
{
    const QString clean = text.simplified();
    if (clean.size() <= kReplyChars)
        return clean;
    // Prefer ending on a sentence boundary if there's one reasonably late.
    const QString cut = clean.left(kReplyChars);
    const qsizetype stop = std::max({cut.lastIndexOf(". "_L1), cut.lastIndexOf("? "_L1), cut.lastIndexOf("! "_L1)});
    if (stop > kReplyChars / 2)
        return cut.left(stop + 1);
    // Otherwise drop the partial last word and mark the cut.
    static const QRegularExpression partialWord(u"\\s+\\S*$"_s);
    return QString(cut).remove(partialWord) + u'…';
}

// key=value pairs, two spaces apart, strings bare and everything else as
// compact JSON -- the same summary util.js formatArgs() builds. One known
// difference: QJsonObject keeps keys sorted, JS keeps insertion order, so a
// multi-argument call can list its arguments in a different order.
QString OverlayModel::formatArgs(const QJsonObject &args, int limit)
{
    QStringList parts;
    for (auto it = args.begin(); it != args.end(); ++it) {
        const QJsonValue value = it.value();
        QString text;
        if (value.isString()) {
            text = value.toString();
        } else {
            // QJsonDocument can only serialise containers; wrap and unwrap.
            const QByteArray json = QJsonDocument(QJsonArray{value}).toJson(QJsonDocument::Compact);
            text = QString::fromUtf8(json.mid(1, json.size() - 2));
        }
        parts << it.key() + u'=' + text;
    }
    const QString joined = parts.join("  "_L1);
    return joined.size() > limit ? joined.left(limit - 1) + u'…' : joined;
}
