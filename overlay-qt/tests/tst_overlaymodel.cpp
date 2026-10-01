// OverlayModel: every transition the Electron overlay (overlay.js) makes,
// driven with the same JSON the agent sends.

#include "OverlayModel.h"

#include <QElapsedTimer>
#include <QJsonArray>
#include <QJsonObject>
#include <QSignalSpy>
#include <QTest>

using namespace Qt::StringLiterals;

namespace {

QJsonObject voiceState(const QString &state, bool session)
{
    return {{u"type"_s, u"voice_state"_s}, {u"state"_s, state}, {u"session"_s, session}};
}

QJsonObject sessionStarted()
{
    return {{u"type"_s, u"session_started"_s}, {u"trigger"_s, u"wake_word"_s}};
}

QJsonObject sessionEnded(const QString &reason)
{
    return {{u"type"_s, u"session_ended"_s}, {u"reason"_s, reason}};
}

QJsonObject level(const QJsonValue &value)
{
    return {{u"type"_s, u"audio_level"_s}, {u"level"_s, value}};
}

QJsonObject confirmationRequired(const QString &id, const QJsonObject &args = {{u"cmd"_s, u"rm -rf build"_s}})
{
    return {{u"type"_s, u"confirmation_required"_s}, {u"id"_s, id}, {u"tool"_s, u"run_command"_s}, {u"args"_s, args}};
}

QJsonObject confirmationResolved(const QString &id, bool approved, const QString &by)
{
    return {{u"type"_s, u"confirmation_resolved"_s}, {u"id"_s, id}, {u"approved"_s, approved}, {u"by"_s, by}};
}

} // namespace

class TestOverlayModel : public QObject
{
    Q_OBJECT

private slots:
    // --- session lifecycle -------------------------------------------------

    void startsHidden()
    {
        OverlayModel model;
        QVERIFY(!model.isVisible());
        QVERIFY(!model.inSession());
        QVERIFY(!model.confirmationVisible());
        QVERIFY(!model.isInteractive());
    }

    void sessionStartedShowsListening()
    {
        OverlayModel model;
        QSignalSpy visible(&model, &OverlayModel::visibleChanged);
        model.handleMessage(sessionStarted());
        QVERIFY(model.isVisible());
        QVERIFY(model.inSession());
        QCOMPARE(visible.count(), 1);
        QCOMPARE(model.state(), u"listening"_s);
        QCOMPARE(model.label(), u"Listening"_s);
        QCOMPARE(model.line(), u"Go ahead. Say “thanks” when you're done."_s);
        QVERIFY(model.lineMuted());
    }

    // A client that connects mid-session never sees session_started, only
    // the current voice_state.
    void voiceStateWithSessionStartsOne()
    {
        OverlayModel model;
        model.handleMessage(voiceState(u"thinking"_s, true));
        QVERIFY(model.isVisible());
        QVERIFY(model.inSession());
        QCOMPARE(model.state(), u"thinking"_s);
    }

    void idleAndOffWithoutSessionStayHidden()
    {
        OverlayModel model;
        model.handleMessage(voiceState(u"idle"_s, false));
        model.handleMessage(voiceState(u"off"_s, false));
        QVERIFY(!model.isVisible());
    }

    void statesShowTheirLabelAndLine_data()
    {
        QTest::addColumn<QString>("state");
        QTest::addColumn<QString>("label");
        QTest::addColumn<QString>("line");
        QTest::addColumn<bool>("muted");
        QTest::newRow("listening") << u"listening"_s << u"Listening"_s << u"Go ahead. Say “thanks” when you're done."_s << true;
        QTest::newRow("hearing") << u"hearing"_s << u"Hearing you"_s << u"…"_s << true;
        QTest::newRow("transcribing") << u"transcribing"_s << u"Transcribing"_s << u"Catching that…"_s << true;
        QTest::newRow("thinking") << u"thinking"_s << u"Thinking"_s << u"Working on it…"_s << true;
        QTest::newRow("speaking") << u"speaking"_s << u"Speaking"_s << u"…"_s << true;
        QTest::newRow("awaiting_confirmation") << u"awaiting_confirmation"_s << u"Needs your OK"_s
                                               << u"Waiting for your answer"_s << false;
    }

    void statesShowTheirLabelAndLine()
    {
        QFETCH(QString, state);
        QFETCH(QString, label);
        QFETCH(QString, line);
        QFETCH(bool, muted);
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(voiceState(state, true));
        QCOMPARE(model.state(), state);
        QCOMPARE(model.label(), label);
        QCOMPARE(model.line(), line);
        QCOMPARE(model.lineMuted(), muted);
    }

    // "idle"/"off" can't be drawn; a session:true with one of them (or a
    // state from a future protocol) keeps the current look.
    void nonVisualStateInSessionIsIgnored()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(voiceState(u"thinking"_s, true));
        model.handleMessage(voiceState(u"idle"_s, true));
        model.handleMessage(voiceState(u"daydreaming"_s, true));
        QCOMPARE(model.state(), u"thinking"_s);
    }

    void thinkingQuotesTheVoiceTranscript()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage({{u"type"_s, u"transcript"_s}, {u"text"_s, u"open notes"_s}, {u"origin"_s, u"voice_session"_s}});
        model.handleMessage(voiceState(u"thinking"_s, true));
        QCOMPARE(model.line(), u"“open notes”"_s);
        QVERIFY(!model.lineMuted());
    }

    void pushToTalkTranscriptIsNotOurs()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage({{u"type"_s, u"transcript"_s}, {u"text"_s, u"typed"_s}, {u"origin"_s, u"push_to_talk"_s}});
        model.handleMessage({{u"type"_s, u"transcript"_s}, {u"text"_s, u"no origin"_s}});
        QVERIFY(model.transcript().isEmpty());
    }

    void transcriptOutsideSessionIsIgnored()
    {
        OverlayModel model;
        model.handleMessage({{u"type"_s, u"transcript"_s}, {u"text"_s, u"x"_s}, {u"origin"_s, u"voice_session"_s}});
        QVERIFY(model.transcript().isEmpty());
    }

    void replyShowsWhileSpeakingThenListening()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage({{u"type"_s, u"speaking"_s}, {u"state"_s, u"started"_s}, {u"text"_s, u"It is 3 pm."_s}});
        model.handleMessage(voiceState(u"speaking"_s, true));
        QCOMPARE(model.line(), u"It is 3 pm."_s);
        QVERIFY(!model.lineMuted());
        model.handleMessage({{u"type"_s, u"speaking"_s}, {u"state"_s, u"ended"_s}});
        model.handleMessage(voiceState(u"listening"_s, true));
        QCOMPARE(model.line(), u"It is 3 pm."_s);
        QVERIFY(model.lineMuted()); // listening always draws as a hint
    }

    void hearingClearsTheOldReply()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage({{u"type"_s, u"speaking"_s}, {u"state"_s, u"started"_s}, {u"text"_s, u"Old reply."_s}});
        model.handleMessage(voiceState(u"hearing"_s, true));
        model.handleMessage(voiceState(u"listening"_s, true));
        QCOMPARE(model.line(), u"Go ahead. Say “thanks” when you're done."_s);
    }

    void newSessionForgetsTheLastOne()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage({{u"type"_s, u"transcript"_s}, {u"text"_s, u"x"_s}, {u"origin"_s, u"voice_session"_s}});
        model.handleMessage({{u"type"_s, u"speaking"_s}, {u"state"_s, u"started"_s}, {u"text"_s, u"y"_s}});
        model.handleMessage(voiceState(u"thinking"_s, true));
        model.handleMessage(sessionEnded(u"dismissed"_s));
        model.handleMessage(sessionStarted());
        QCOMPARE(model.state(), u"listening"_s);
        QVERIFY(model.transcript().isEmpty());
        QVERIFY(model.replyText().isEmpty());
        QCOMPARE(model.label(), u"Listening"_s);
    }

    // --- ending and the linger ---------------------------------------------

    void endedReasonsLabel_data()
    {
        QTest::addColumn<QString>("reason");
        QTest::addColumn<QString>("label");
        QTest::newRow("dismissed") << u"dismissed"_s << u"Talk soon"_s;
        QTest::newRow("timeout") << u"timeout"_s << u"Stopped listening"_s;
        QTest::newRow("manual") << u"manual"_s << u"Stopped listening"_s;
        QTest::newRow("error") << u"error"_s << u"Something went wrong"_s;
        QTest::newRow("unknown") << u"cosmic ray"_s << u"Stopped listening"_s;
        QTest::newRow("missing") << QString() << u"Stopped listening"_s;
    }

    void endedReasonsLabel()
    {
        QFETCH(QString, reason);
        QFETCH(QString, label);
        OverlayModel model;
        model.handleMessage(sessionStarted());
        QJsonObject ended{{u"type"_s, u"session_ended"_s}};
        if (!reason.isNull())
            ended.insert(u"reason"_s, reason);
        model.handleMessage(ended);
        QCOMPARE(model.label(), label);
        QVERIFY(!model.inSession());
        // The label survives the idle voice_state that follows session_ended.
        model.handleMessage(voiceState(u"idle"_s, false));
        QCOMPARE(model.label(), label);
    }

    void endLingersThenHides()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        QSignalSpy visible(&model, &OverlayModel::visibleChanged);
        QElapsedTimer clock;
        clock.start();
        model.handleMessage(sessionEnded(u"dismissed"_s));
        model.handleMessage(voiceState(u"idle"_s, false));
        QVERIFY(model.isVisible()); // still up for the fade-out
        QVERIFY(visible.wait(OverlayModel::kLingerMs * 3));
        QVERIFY(!model.isVisible());
        // QTimer's default CoarseTimer may fire up to 5% early, by design
        // (and fine here: the fade is 400 ms). Anything earlier is a bug.
        QVERIFY2(clock.elapsed() >= OverlayModel::kLingerMs * 0.95 - 1, qPrintable(QString::number(clock.elapsed())));
    }

    void voiceStateWithoutSessionEndsAMissedEnd()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(voiceState(u"idle"_s, false));
        QVERIFY(!model.inSession());
        QCOMPARE(model.label(), u"Stopped listening"_s);
        QTRY_VERIFY_WITH_TIMEOUT(!model.isVisible(), OverlayModel::kLingerMs * 3);
    }

    void restartDuringLingerStaysVisible()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(sessionEnded(u"manual"_s));
        QSignalSpy visible(&model, &OverlayModel::visibleChanged);
        model.handleMessage(sessionStarted());
        QTest::qWait(OverlayModel::kLingerMs + 150);
        QVERIFY(model.isVisible());
        QCOMPARE(visible.count(), 0); // never flickered off
        QCOMPARE(model.label(), u"Listening"_s);
    }

    void losingTheAgentEndsTheSession()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleDisconnected();
        QVERIFY(!model.inSession());
        QCOMPARE(model.label(), u"Something went wrong"_s);
        QTRY_VERIFY_WITH_TIMEOUT(!model.isVisible(), OverlayModel::kLingerMs * 3);
    }

    void disconnectWhileIdleIsANoOp()
    {
        OverlayModel model;
        QSignalSpy visible(&model, &OverlayModel::visibleChanged);
        model.handleDisconnected();
        QCOMPARE(visible.count(), 0);
    }

    // --- level ---------------------------------------------------------------

    void levelAttacksFastReleasesSlow()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(level(1.0));
        const double afterRise = model.level();
        QVERIFY(afterRise > 0.5 && afterRise < 1.0); // most of the way, not all
        model.handleMessage(level(0.0));
        const double afterFall = model.level();
        const double dropped = afterRise - afterFall;
        QVERIFY(dropped > 0);
        QVERIFY2(dropped < afterRise * 0.5, "release should be slower than attack");
        // Converges on a steady input.
        for (int i = 0; i < 40; ++i)
            model.handleMessage(level(0.3));
        QVERIFY(qAbs(model.level() - 0.3) < 1e-3);
    }

    void levelIsClampedAndValidated()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        for (int i = 0; i < 40; ++i)
            model.handleMessage(level(7.5));
        QVERIFY(model.level() <= 1.0);
        QVERIFY(qAbs(model.level() - 1.0) < 1e-3);
        for (int i = 0; i < 40; ++i)
            model.handleMessage(level(-3));
        QVERIFY(model.level() >= 0.0);
        const double before = model.level();
        model.handleMessage(level(u"loud"_s));
        model.handleMessage(level(QJsonValue::Null));
        model.handleMessage({{u"type"_s, u"audio_level"_s}});
        QCOMPARE(model.level(), before);
    }

    void levelOnlyWhileTheMicIsLive()
    {
        OverlayModel model;
        model.handleMessage(level(0.8)); // no session
        QCOMPARE(model.level(), 0.0);

        model.handleMessage(sessionStarted());
        model.handleMessage(level(0.8));
        QVERIFY(model.level() > 0);
        model.handleMessage(voiceState(u"thinking"_s, true));
        QCOMPARE(model.level(), 0.0); // reset on leaving a live-mic state
        model.handleMessage(level(0.8));
        QCOMPARE(model.level(), 0.0); // and stray ticks are dropped

        model.handleMessage(voiceState(u"awaiting_confirmation"_s, true));
        model.handleMessage(level(0.8));
        QVERIFY(model.level() > 0); // the mic is live for a spoken yes/no
    }

    // --- confirmation card -------------------------------------------------

    void cardOnlyDuringASession()
    {
        OverlayModel model;
        model.handleMessage(confirmationRequired(u"c1"_s));
        QVERIFY(!model.confirmationVisible());
        QVERIFY(!model.isInteractive());
    }

    void cardShowsTheCall()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        QSignalSpy interactive(&model, &OverlayModel::interactiveChanged);
        model.handleMessage(confirmationRequired(u"c1"_s));
        model.handleMessage(voiceState(u"awaiting_confirmation"_s, true));
        QVERIFY(model.confirmationVisible());
        QVERIFY(model.isInteractive());
        QCOMPARE(interactive.count(), 1);
        QCOMPARE(interactive.first().first().toBool(), true);
        QCOMPARE(model.confirmationId(), u"c1"_s);
        QCOMPARE(model.confirmationTool(), u"run_command"_s);
        QCOMPARE(model.confirmationCommand(), u"run_command(cmd=rm -rf build)"_s);
        QCOMPARE(model.line(), u"Okay to run run_command?"_s);
    }

    void answeringSendsAndSettles()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        QSignalSpy requested(&model, &OverlayModel::confirmRequested);
        QSignalSpy interactive(&model, &OverlayModel::interactiveChanged);

        model.answer(true);
        QCOMPARE(requested.count(), 1);
        QCOMPARE(requested.first().at(0).toString(), u"c1"_s);
        QCOMPARE(requested.first().at(1).toBool(), true);
        // Click-through again at once; the card stays to show the outcome.
        QVERIFY(!model.isInteractive());
        QCOMPARE(interactive.count(), 1);
        QVERIFY(model.confirmationVisible());
        QVERIFY(model.confirmationAnswered());
        QVERIFY(model.confirmationApproved());
        QCOMPARE(model.confirmationOutcome(), u"Approved"_s);

        // A second click (or a double click) can't send twice.
        model.answer(false);
        QCOMPARE(requested.count(), 1);

        // The agent's echo of our own answer changes nothing.
        model.handleMessage(confirmationResolved(u"c1"_s, true, u"overlay"_s));
        QCOMPARE(model.confirmationOutcome(), u"Approved"_s);

        QTRY_VERIFY_WITH_TIMEOUT(!model.confirmationVisible(), OverlayModel::kSettleMs * 3);
    }

    void declining()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        QSignalSpy requested(&model, &OverlayModel::confirmRequested);
        model.answer(false);
        QCOMPARE(requested.first().at(1).toBool(), false);
        QCOMPARE(model.confirmationOutcome(), u"Declined"_s);
        QVERIFY(!model.confirmationApproved());
    }

    void resolvedElsewhere_data()
    {
        QTest::addColumn<QString>("by");
        QTest::addColumn<bool>("approved");
        QTest::addColumn<QString>("outcome");
        QTest::newRow("voice") << u"voice"_s << true << u"Approved by voice"_s;
        QTest::newRow("main window") << u"ui"_s << false << u"Declined in the main window"_s;
        QTest::newRow("no by") << QString() << true << u"Approved in the main window"_s;
        QTest::newRow("another overlay") << u"overlay"_s << true << u"Approved on the other overlay"_s;
    }

    void resolvedElsewhere()
    {
        QFETCH(QString, by);
        QFETCH(bool, approved);
        QFETCH(QString, outcome);
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        QSignalSpy requested(&model, &OverlayModel::confirmRequested);

        QJsonObject resolved{{u"type"_s, u"confirmation_resolved"_s}, {u"id"_s, u"c1"_s}, {u"approved"_s, approved}};
        if (!by.isNull())
            resolved.insert(u"by"_s, by);
        model.handleMessage(resolved);

        QCOMPARE(requested.count(), 0); // not ours to send
        QVERIFY(model.confirmationAnswered());
        QVERIFY(!model.isInteractive());
        QCOMPARE(model.confirmationOutcome(), outcome);
        QTRY_VERIFY_WITH_TIMEOUT(!model.confirmationVisible(), OverlayModel::kSettleMs * 3);
    }

    void resolvedForAnotherIdIsIgnored()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        model.handleMessage(confirmationResolved(u"c2"_s, true, u"voice"_s));
        QVERIFY(model.isInteractive());
    }

    void responseClosesAnUnansweredCard()
    {
        // e.g. the agent's 120 s confirmation timeout: no resolved, just the
        // command's response.
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        model.handleMessage({{u"type"_s, u"response"_s}, {u"text"_s, u"Timed out."_s}, {u"origin"_s, u"voice_session"_s}});
        QVERIFY(!model.confirmationVisible());
        QVERIFY(!model.isInteractive());
    }

    void responseLeavesAnAnsweredCardToSettle()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        model.answer(true);
        model.handleMessage({{u"type"_s, u"response"_s}, {u"text"_s, u""_s}});
        QVERIFY(model.confirmationVisible()); // outcome still readable
    }

    void sessionEndClosesTheCard()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        model.handleMessage(sessionEnded(u"manual"_s));
        QVERIFY(!model.confirmationVisible());
        QVERIFY(!model.isInteractive());
        model.answer(true); // nothing to answer any more
    }

    void newRequestReplacesAnAnsweredCard()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(confirmationRequired(u"c1"_s));
        model.answer(true);
        model.handleMessage(confirmationRequired(u"c2"_s));
        QVERIFY(model.isInteractive());
        QCOMPARE(model.confirmationId(), u"c2"_s);
        // The first card's settle timer must not close the second one.
        QTest::qWait(OverlayModel::kSettleMs + 200);
        QVERIFY(model.confirmationVisible());
    }

    void malformedRequestsAreIgnored()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage({{u"type"_s, u"confirmation_required"_s}, {u"tool"_s, u"run_command"_s}}); // no id
        model.handleMessage({{u"type"_s, u"confirmation_required"_s}, {u"id"_s, 7}, {u"tool"_s, u"x"_s}});
        model.handleMessage({{u"type"_s, u"confirmation_required"_s}, {u"id"_s, u"c1"_s}}); // no tool
        QVERIFY(!model.confirmationVisible());
        // args that aren't an object just summarise as empty
        model.handleMessage({{u"type"_s, u"confirmation_required"_s}, {u"id"_s, u"c1"_s}, {u"tool"_s, u"t"_s}, {u"args"_s, u"oops"_s}});
        QCOMPARE(model.confirmationCommand(), u"t()"_s);
        model.handleMessage({{u"type"_s, u"confirmation_resolved"_s}, {u"id"_s, u"c1"_s}, {u"approved"_s, u"yes"_s}});
        QVERIFY(model.isInteractive());
    }

    // --- garbage -------------------------------------------------------------

    void garbageIsIgnored()
    {
        OverlayModel model;
        model.handleMessage(sessionStarted());
        model.handleMessage(voiceState(u"thinking"_s, true));
        QSignalSpy display(&model, &OverlayModel::displayChanged);
        QSignalSpy visible(&model, &OverlayModel::visibleChanged);

        model.handleMessage({});
        model.handleMessage({{u"type"_s, 42}});
        model.handleMessage({{u"type"_s, u"skill_proposed"_s}, {u"id"_s, u"s"_s}});
        model.handleMessage({{u"type"_s, u"control_result"_s}, {u"ok"_s, true}});
        model.handleMessage({{u"type"_s, u"voice_state"_s}, {u"state"_s, u"listening"_s}}); // no session
        model.handleMessage({{u"type"_s, u"voice_state"_s}, {u"state"_s, u"listening"_s}, {u"session"_s, u"yes"_s}});
        model.handleMessage({{u"type"_s, u"voice_state"_s}, {u"state"_s, 3}, {u"session"_s, true}});
        model.handleMessage({{u"type"_s, u"speaking"_s}, {u"state"_s, u"started"_s}, {u"text"_s, QJsonArray{1, 2}}});
        model.handleMessage({{u"type"_s, u"transcript"_s}, {u"origin"_s, u"voice_session"_s}, {u"text"_s, QJsonObject{}}});

        QVERIFY(model.inSession());
        QCOMPARE(model.state(), u"thinking"_s);
        QCOMPARE(visible.count(), 0);
        // The transcript with a non-string text became "", which is a real
        // (if pointless) update; nothing else may have changed the display.
        QVERIFY(display.count() <= 1);
        QCOMPARE(model.line(), u"Working on it…"_s);
    }

    // --- text shaping ------------------------------------------------------

    void shortReply_data()
    {
        QTest::addColumn<QString>("in");
        QTest::addColumn<QString>("out");
        QTest::newRow("short") << u"  Hello   there.\n"_s << u"Hello there."_s;
        const QString sentence = u"This first sentence is comfortably long enough to pass the halfway mark of the limit. "_s;
        QTest::newRow("sentence cut") << sentence + QString(100, u'x') << sentence.trimmed();
        // The 150-char cut lands mid-word ("wor"); that fragment is dropped.
        // Expected value computed with overlay.js's own shortReply in node.
        const QString words = u"a"_s + QString(u"word "_s).repeated(40);
        QTest::newRow("word cut") << words << u"a"_s + QString(u"word "_s).repeated(29).trimmed() + u'…';
    }

    void shortReply()
    {
        QFETCH(QString, in);
        QFETCH(QString, out);
        const QString result = OverlayModel::shortReply(in);
        QCOMPARE(result, out);
        QVERIFY(result.size() <= OverlayModel::kReplyChars + 1);
    }

    void formatArgs()
    {
        QCOMPARE(OverlayModel::formatArgs({}, 200), QString());
        QCOMPARE(OverlayModel::formatArgs({{u"cmd"_s, u"ls"_s}}, 200), u"cmd=ls"_s);
        QCOMPARE(OverlayModel::formatArgs({{u"n"_s, 3}, {u"on"_s, true}, {u"x"_s, QJsonValue::Null}}, 200),
                 u"n=3  on=true  x=null"_s);
        QCOMPARE(OverlayModel::formatArgs({{u"list"_s, QJsonArray{1, u"a"_s}}}, 200), u"list=[1,\"a\"]"_s);
        const QString cut = OverlayModel::formatArgs({{u"cmd"_s, QString(300, u'a')}}, 200);
        QCOMPARE(cut.size(), 200);
        QVERIFY(cut.endsWith(u'…'));
    }
};

QTEST_GUILESS_MAIN(TestOverlayModel)
#include "tst_overlaymodel.moc"
