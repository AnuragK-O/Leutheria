// Non-Apple builds: the overlay still runs (useful for working on the QML),
// it just behaves like an ordinary always-on-top frameless window.
#include "MacWindow.h"

#include <QWindow>

namespace MacWindow {

void neverActivate() {}

void configure(QWindow *) {}

void showInactive(QWindow *window)
{
    window->show();
}

void hide(QWindow *window)
{
    window->hide();
}

void setInteractive(QWindow *window, bool interactive)
{
    window->setFlag(Qt::WindowTransparentForInput, !interactive);
}

} // namespace MacWindow
