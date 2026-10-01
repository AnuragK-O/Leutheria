#include "AssetManifest.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLoggingCategory>

#include <algorithm>

Q_LOGGING_CATEGORY(lcManifest, "leutheria.manifest")

using namespace Qt::StringLiterals;

namespace {

constexpr int kMinSlot = 32;
constexpr int kMaxSlot = 120;

bool isInside(const QString &path, const QString &dir)
{
    return path.startsWith(dir + u'/');
}

} // namespace

const QStringList &AssetManifest::states()
{
    static const QStringList kStates{u"listening"_s, u"hearing"_s,  u"transcribing"_s,
                                     u"thinking"_s,  u"speaking"_s, u"awaiting_confirmation"_s};
    return kStates;
}

AssetManifest::AssetManifest(QObject *parent)
    : QObject(parent)
{
    for (const QString &state : states())
        m_entries[state] = Entry{};
}

void AssetManifest::load(const QString &dir)
{
    m_problems.clear();
    m_slotSize = kDefaultSlotSize;
    for (const QString &state : states())
        m_entries[state] = Entry{};
    parse(dir);
    emit changed();
}

void AssetManifest::parse(const QString &dir)
{
    const QString manifestPath = QDir(dir).filePath(u"manifest.json"_s);
    QFile file(manifestPath);
    if (!file.open(QIODevice::ReadOnly)) {
        report(u"manifest unreadable (%1: %2), using placeholders"_s.arg(manifestPath, file.errorString()));
        return;
    }
    QJsonParseError error{};
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll(), &error);
    if (error.error != QJsonParseError::NoError || !doc.isObject()) {
        report(u"manifest unreadable (%1), using placeholders"_s.arg(
            error.error != QJsonParseError::NoError ? error.errorString() : u"not a JSON object"_s));
        return;
    }
    const QJsonObject raw = doc.object();

    // main.js passes `size` through and overlay.js clamps it; do both here.
    if (const QJsonValue size = raw.value("size"_L1); size.isDouble() && size.toDouble() > 0)
        m_slotSize = std::clamp(qRound(size.toDouble()), kMinSlot, kMaxSlot);

    // Canonical form of the folder, so symlinks can't be used to step out of
    // it (stricter than main.js's path.resolve(), which only catches "..").
    const QString root = QFileInfo(dir).canonicalFilePath();
    const QJsonObject rawStates = raw.value("states"_L1).toObject();

    for (const QString &state : states()) {
        // Absent (or null) means placeholder, silently -- that's the default.
        if (rawStates.value(state).isUndefined() || rawStates.value(state).isNull())
            continue;
        const QJsonObject item = rawStates.value(state).toObject();
        const QString type = item.value("type"_L1).toString();
        const QString src = item.value("src"_L1).toString();

        if (type == "css"_L1)
            continue;
        if (type != "image"_L1 && type != "video"_L1) {
            report(u"%1: unknown type \"%2\", using placeholder"_s.arg(state, type));
            continue;
        }
        if (src.isEmpty()) {
            report(u"%1: no src, using placeholder"_s.arg(state));
            continue;
        }

        // Resolve lexically first (catches "../x" and "/etc/x" even when the
        // target doesn't exist), then canonically (catches symlinks).
        const QString lexical = QDir::cleanPath(QDir(dir).absoluteFilePath(src));
        const QString canonical = QFileInfo(lexical).canonicalFilePath();
        if (!isInside(lexical, QDir::cleanPath(QDir(dir).absolutePath())) || canonical.isEmpty()
            || root.isEmpty() || !isInside(canonical, root) || !QFileInfo(canonical).isFile()) {
            report(u"%1: %2 not found in assets/, using placeholder"_s.arg(state, src));
            continue;
        }

        if (type == "video"_L1) {
            const QString suffix = QFileInfo(canonical).suffix().toLower();
            if (m_videoSupport && !m_videoSupport(suffix)) {
                report(u"%1: this platform's media backend can't play .%2 video (%3), using placeholder"_s.arg(
                    state, suffix, src));
                continue;
            }
        }

        Entry entry;
        entry.type = type == "video"_L1 ? Type::Video : Type::Image;
        entry.file = canonical;
        entry.loop = item.value("loop"_L1).toBool(true);
        const QString react = item.value("react"_L1).toString();
        entry.react = (react == "scale"_L1 || react == "glow"_L1) ? react : u"none"_s;
        m_entries[state] = entry;
    }
}

AssetManifest::Entry AssetManifest::entry(const QString &state) const
{
    const auto it = m_entries.find(state);
    return it != m_entries.end() ? it->second : Entry{};
}

QVariantMap AssetManifest::entries() const
{
    QVariantMap out;
    for (const auto &[state, entry] : m_entries) {
        const QString type = entry.type == Type::Video   ? u"video"_s
                             : entry.type == Type::Image ? u"image"_s
                                                         : u"css"_s;
        out.insert(state, QVariantMap{
                              {u"type"_s, type},
                              {u"source"_s, entry.file.isEmpty() ? QUrl() : QUrl::fromLocalFile(entry.file)},
                              {u"loop"_s, entry.loop},
                              {u"react"_s, entry.react},
                          });
    }
    return out;
}

void AssetManifest::markBroken(const QString &state, const QString &reason)
{
    const auto it = m_entries.find(state);
    if (it == m_entries.end() || it->second.type == Type::Css)
        return;
    report(u"%1: asset failed to load (%2), using placeholder"_s.arg(state, reason));
    it->second = Entry{};
    emit changed();
}

void AssetManifest::report(const QString &problem)
{
    qCWarning(lcManifest).noquote() << "[overlay] manifest:" << problem;
    m_problems << problem;
}
