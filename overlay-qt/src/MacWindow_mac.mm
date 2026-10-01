#include "MacWindow.h"

#include <QLoggingCategory>
#include <QWindow>

#import <AppKit/AppKit.h>

// Debug-level state dumps are off by default; QT_LOGGING_RULES="leutheria.*.debug=true" turns them on.
Q_LOGGING_CATEGORY(lcWindow, "leutheria.window", QtInfoMsg)

namespace {

NSWindow *nsWindowFor(QWindow *window)
{
    // winId() creates the platform window if needed; on Cocoa it's the NSView
    // Qt draws into, and its window is the NSWindow (a QNSPanel here, because
    // Main.qml asks for Qt::Tool).
    auto *view = reinterpret_cast<NSView *>(window->winId());
    return view.window;
}

// What Qt gives a frameless Qt::Tool window that stays on top (measured,
// Qt 6.11): level 8, collectionBehavior MoveToActiveSpace|FullScreenAuxiliary,
// hidesOnDeactivate YES, no non-activating mask. Qt sets these when it creates
// the NSWindow (and would again if the window flags changed, which they never
// do here), not on show, so applying ours once in configure() sticks.
void applyBehaviour(NSWindow *window)
{
    window.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces
                                | NSWindowCollectionBehaviorFullScreenAuxiliary
                                | NSWindowCollectionBehaviorStationary
                                | NSWindowCollectionBehaviorIgnoresCycle;
    window.hasShadow = NO; // the island draws its own shadow
    window.opaque = NO;
    window.backgroundColor = NSColor.clearColor;

    if ([window isKindOfClass:NSPanel.class]) {
        auto *panel = static_cast<NSPanel *>(window);
        // Qt::Tool panels hide whenever their app deactivates, and this app
        // is never active (neverActivate()), so without this the overlay
        // would never be seen.
        panel.hidesOnDeactivate = NO;
        // A click on a non-activating panel is delivered without activating
        // its app. neverActivate() already guarantees that; this keeps the
        // panel well-behaved on its own, should the policy ever change.
        panel.styleMask |= NSWindowStyleMaskNonactivatingPanel;
        panel.becomesKeyOnlyIfNeeded = YES;
    }

    // Electron's overlay uses setAlwaysOnTop(true, "screen-saver"); same level,
    // so the two overlays stack identically against other apps. Don't add
    // `panel.floatingPanel = YES` above: its setter silently resets the level
    // to NSFloatingWindowLevel (3) -- see BUGS.md.
    window.level = NSScreenSaverWindowLevel;
}

void logState(const char *what, NSWindow *window)
{
    qCDebug(lcWindow).nospace() << what << ": class=" << NSStringFromClass(window.class).UTF8String
                                << " level=" << window.level << " key=" << window.isKeyWindow
                                << " appActive=" << NSApp.isActive << " frontmost="
                                << NSWorkspace.sharedWorkspace.frontmostApplication.localizedName.UTF8String
                                << " ignoresMouse=" << window.ignoresMouseEvents
                                << " nonactivating="
                                << bool(window.styleMask & NSWindowStyleMaskNonactivatingPanel);
}

} // namespace

namespace MacWindow {

void neverActivate()
{
    // Measured on macOS 26 with Qt 6.11 (see README "Focus and clicks"): as Accessory
    // (what LSUIElement gives), Qt's -activateIgnoringOtherApps: at launch
    // made the overlay the frontmost app, and so, a moment later, did
    // ordering the panel front -- the user's app lost focus either way. A
    // Prohibited app can't be activated at all, yet its non-activating panel
    // still orders front and still receives clicks.
    [NSApp setActivationPolicy:NSApplicationActivationPolicyProhibited];
}

void configure(QWindow *window)
{
    NSWindow *ns = nsWindowFor(window);
    if (!ns) {
        qCWarning(lcWindow) << "no NSWindow yet; configure() must run after create()";
        return;
    }
    applyBehaviour(ns);
    ns.ignoresMouseEvents = YES;
    logState("configured", ns);
}

void showInactive(QWindow *window)
{
    // With the app Prohibited (neverActivate) showing can't activate it, and
    // Qt::WindowDoesNotAcceptFocus makes Qt order the panel front without
    // making it key. Both measured: see README "Focus and clicks".
    window->show();
    logState("shown", nsWindowFor(window));
}

void hide(QWindow *window)
{
    window->hide();
}

void setInteractive(QWindow *window, bool interactive)
{
    NSWindow *ns = nsWindowFor(window);
    ns.ignoresMouseEvents = !interactive;
    logState(interactive ? "interactive (card up)" : "click-through", ns);
}

} // namespace MacWindow
