#pragma once

#include <QObject>
#include <QString>
#include <QStringList>
#include <QUrl>
#include <QVariantMap>
#include <QtQml/qqmlregistration.h>

#include <functional>
#include <map>

// Reads app/renderer/overlay/assets/manifest.json -- the same file, and the
// same rules, as main.js loadOverlayManifest() -- so a designer's asset drop
// shows up identically in the Electron and the Qt overlay.
//
// It never fails: an unreadable manifest, an unknown type, a missing src, a
// path that escapes the assets folder, a missing file or (Qt-only) a video
// the platform can't decode each fall back to the built-in placeholder for
// that one state, with a warning saying why. A typo must not blank the overlay.
class AssetManifest : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    QML_UNCREATABLE("Loaded by main.cpp and handed to Main.qml")

    // Side of the square asset slot, clamped to 32..120 (default 56).
    Q_PROPERTY(int slotSize READ slotSize NOTIFY changed)
    // state -> {type: "css"|"image"|"video", source: url, loop: bool,
    //           react: "none"|"scale"|"glow"}. Every visual state is present.
    Q_PROPERTY(QVariantMap entries READ entries NOTIFY changed)

public:
    enum class Type { Css, Image, Video };

    struct Entry
    {
        Type type = Type::Css;
        QString file; // absolute path; empty for Css
        bool loop = true;
        QString react = QStringLiteral("none");
    };

    static constexpr int kDefaultSlotSize = 56;

    // Given a lower-case file suffix, can this platform play that video?
    // main.cpp answers from QMediaFormat; tests stub it.
    using VideoSupport = std::function<bool(const QString &suffix)>;

    explicit AssetManifest(QObject *parent = nullptr);

    void setVideoSupport(VideoSupport support) { m_videoSupport = std::move(support); }

    // Replaces the current entries with what <dir>/manifest.json says.
    void load(const QString &dir);

    Entry entry(const QString &state) const;
    int slotSize() const { return m_slotSize; }
    QVariantMap entries() const;
    // Why each fallback happened, in load order (also logged).
    QStringList problems() const { return m_problems; }

    static const QStringList &states();

public slots:
    // An asset that passed validation but failed at runtime (a corrupt file,
    // a codec the backend rejected): placeholder from now on, like the
    // Electron overlay's <img>/<video> error handler.
    void markBroken(const QString &state, const QString &reason);

signals:
    void changed();

private:
    void parse(const QString &dir);
    void report(const QString &problem);

    std::map<QString, Entry> m_entries;
    int m_slotSize = kDefaultSlotSize;
    QStringList m_problems;
    VideoSupport m_videoSupport;
};
