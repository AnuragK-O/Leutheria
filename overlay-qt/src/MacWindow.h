#pragma once

class QWindow;

// The NSWindow behaviour Qt has no cross-platform API for. Implemented in
// MacWindow_mac.mm; MacWindow_stub.cpp keeps other platforms building (plain
// show/hide, no click-through).
//
// What the overlay needs from the window server, and why:
//  - a level above normal windows, on every Space and over full-screen apps:
//    the user talks to Leutheria *while* using something else;
//  - never activating the app or taking key focus when it appears -- the
//    same reason, the user's typing must keep going where it was going;
//  - click-through, except while the confirmation card is up.
namespace MacWindow {

// Call before QGuiApplication::exec(). The overlay's app must never become
// the active app: not at launch (Qt's delegate activates any app launched
// while something else is frontmost, which is exactly how Electron spawns
// this one), not when the panel appears, not when the card is clicked.
void neverActivate();

// Once, after the QWindow exists and before it is first shown.
void configure(QWindow *window);

// Order the window front without activating the app or making it key.
void showInactive(QWindow *window);

void hide(QWindow *window);

// false: clicks pass straight through to whatever is underneath.
// true: the window receives mouse events (the confirmation card is up).
void setInteractive(QWindow *window, bool interactive);

} // namespace MacWindow
