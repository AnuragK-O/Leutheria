# LeutheriaOverlay (C++ / Qt Quick)

A native implementation of Leutheria's activation overlay: the small floating "island"
at the top of the screen that shows a voice session's state (listening, hearing,
transcribing, thinking, speaking, waiting for approval), what you said, a short form of
the reply, and a compact Approve / Decline card when the agent needs a yes or no.

It is a second client of the agent's existing WebSocket protocol. It draws the same thing
as the Electron overlay in `app/renderer/overlay/`, from the same messages and the same
asset manifest. Electron's overlay is still the default; the Python agent and Electron
work unchanged whether or not this one is running.

## Build, test, run

Requires Qt 6.5 or later (developed against Homebrew `qt` 6.11), CMake 3.21 or later,
and Ninja.

```bash
cmake -S overlay-qt -B overlay-qt/build -G Ninja -DCMAKE_PREFIX_PATH=/opt/homebrew/opt/qt
cmake --build overlay-qt/build
ctest --test-dir overlay-qt/build            # 3 Qt Test suites, about 10 s
cmake --build overlay-qt/build --target all_qmllint   # optional, currently clean
```

Configuring also writes `overlay-qt/build/compile_commands.json`. The repo's
`.vscode/settings.json` points VS Code's C/C++ extension at it; without it the editor
can't find Homebrew's Qt headers and flags every `#include <Q...>` as missing, though the
build is fine. Configure once, then reload the window. clangd users: pass
`--compile-commands-dir=overlay-qt/build`.

The output is `overlay-qt/build/LeutheriaOverlay.app`. Its `Info.plist` sets
`LSUIElement`, so it has no Dock icon and no menu bar.

```bash
# Against the real agent (the default URL)
overlay-qt/build/LeutheriaOverlay.app/Contents/MacOS/LeutheriaOverlay

# Against the scripted mock, no Python, mic or API key needed
cd app && node scripts/mock-agent.js &      # port 8799; plays a session with a confirmation
../overlay-qt/build/LeutheriaOverlay.app/Contents/MacOS/LeutheriaOverlay --url ws://127.0.0.1:8799
```

| option | meaning |
|---|---|
| `--url <ws://...>` | Agent WebSocket. Default `ws://127.0.0.1:8765`. |
| `--assets <dir>` | Folder holding `manifest.json`. Default: `app/renderer/overlay/assets`, found by walking up from the executable. |
| `--theme light\|dark` | Force an appearance. Default: follow the macOS appearance (`Application.styleHints.colorScheme`). |
| `--capture-dir <dir>` | Debug: write a PNG of the overlay on every state change (`03-hearing.png`, `06-confirm-card.png`, ...), named like the Electron capture path's frames so the two can be compared side by side. |

Electron launches it when `LEUTHERIA_OVERLAY=qt` is set (with `--url` and `--assets`),
and skips its own overlay in that case.

Logging uses Qt categories under `leutheria.*`. Per-event detail (window state, socket
retries) is at debug level and off by default:
`QT_LOGGING_RULES="leutheria.*.debug=true"`.

## Architecture

The rule is that C++ owns the logic and QML owns the pixels. QML never decides *whether*
something is shown, only *how* it looks.

```
 AgentClient ──messageReceived(QJsonObject)──▶ OverlayModel ──Q_PROPERTYs──▶ Main.qml
      ▲                                           │  visibleChanged      Island, ConfirmCard,
      └──────────── sendConfirm(id, approved) ◀───┘  interactiveChanged  AssetSlot, Orb
                                                         │
                                         main.cpp ───────┴──▶ MacWindow (.mm): show without
                                                                activating, click-through
 AssetManifest (manifest.json) ──entries / slotSize──▶ AssetSlot
```

| file | responsibility |
|---|---|
| `src/AgentClient.{h,cpp}` | The `QWebSocket`. Sends `{"type":"hello","role":"overlay"}` first on every connection, reconnects forever with capped exponential backoff (0.5 s doubling to 8 s), drops anything that isn't a JSON object with a string `type`, and sends `confirm`. It never interprets a message. |
| `src/OverlayModel.{h,cpp}` | Every rule about what the overlay shows, ported from `overlay.js`: session start and end, the 650 ms linger after a session ends, label and line text per state, reply shortening, level smoothing, and the confirmation card's lifecycle. Exposed to QML as properties; no QML or socket dependency, so the tests drive it with plain `QJsonObject`s. |
| `src/AssetManifest.{h,cpp}` | Loads `manifest.json` with the same validation and fallbacks as `main.js loadOverlayManifest()`. Any problem costs only that one state, which falls back to the built-in orb with a logged reason. |
| `src/MacWindow.h`, `MacWindow_mac.mm` | The Objective-C++ layer: activation policy, window level, Spaces behaviour, click-through. `MacWindow_stub.cpp` keeps non-Apple builds compiling (QML can be worked on anywhere). |
| `src/FrameCapture.{h,cpp}` | `--capture-dir` only: `QQuickWindow::grabWindow()` 500 ms after each state change. |
| `src/main.cpp` | CLI parsing and wiring, nothing else. |
| `qml/Main.qml` | The transparent 460x300 window and the fade in/out "stage". |
| `qml/Island.qml`, `ConfirmCard.qml`, `OverlayButton.qml`, `Icon.qml` | Ports of the matching `overlay.css` / `styles.css` rules. `Icon.qml` draws the same 24x24 SVG paths as `util.js icon()`. |
| `qml/AssetSlot.qml`, `Orb.qml` | The asset slot: the manifest's image or video, or the placeholder orb, which has a distinct look per state driven by the mic level. Animations only drive "phase" properties; every visual property is a binding, so no animation's last value can stick after a state change. |
| `qml/Theme.qml` | The palette, as literal values copied from the two CSS files, so a diff against them stays easy. |

**Build layout.** `overlay_core` is a static library holding the three logic classes,
linked by both the app and the tests, so the tests exercise exactly what ships. It is also
a QML module (`Leutheria.Core`): `QML_ELEMENT` gives QML and qmllint real types for
`OverlayModel` and `AssetManifest`, which `Main.qml` receives as required properties via
`setInitialProperties`. The app's own QML is a second module, `Leutheria.Overlay`, loaded
with `loadFromModule`.

### The manifest

Same file, same rules as Electron:

- A state that is missing or `css` gets the placeholder orb.
- Anything else falls back for that state only, with a log line saying why: an unknown
  `type`, no `src`, a `src` that resolves outside the assets folder, or a file that
  doesn't exist.
- `size` is clamped to 32..120 and defaults to 56.

Two differences, both on the strict side:

- Paths are also checked after resolving symlinks, so a symlink inside `assets/` can't
  point out of it.
- A video in a container that Qt Multimedia's backend can't decode is rejected up front.
  That decision comes from `QMediaFormat::supportedFileFormats(Decode)`, not a hard-coded
  list.

A file that passes validation but fails at runtime (a corrupt PNG, a truncated MP4) is
reported by the QML through `AssetManifest::markBroken()` and swapped for the orb.

### Focus and clicks (measured on macOS 26, Qt 6.11)

The overlay must never take focus from the app the user is working in. What it took to
get there:

- **Launch.** Qt's application delegate calls `-activateIgnoringOtherApps:` at launch
  whenever another app is frontmost (`qt.qpa.application: Launched with <...> as frontmost
  application. Activating <...> instead`). That is meant to keep terminal-launched apps
  from opening behind the terminal. Electron spawns this binary directly, so it would
  steal focus on every start.
- **Showing.** With the policy left at Accessory (what `LSUIElement` gives), ordering the
  panel front made the overlay the frontmost app about 0.7 s later. This happened with
  `QWindow::show()`, with `_q_showWithoutActivating`, and with a raw
  `-orderFrontRegardless`.
- **Fix.** `MacWindow::neverActivate()` sets `NSApplicationActivationPolicyProhibited`
  before the event loop starts. A Prohibited app can't be activated at all, and its
  non-activating panel still appears, at level 1000, on every Space.
- **Clicks.** A synthesized click on Approve reached the button: the agent received
  `{"type":"confirm","id":...,"approved":true,"remember":null}` and the card settled to
  "Approved". The frontmost app was the same before the click, 0.3 s after it and 1 s
  after it. The panel never became key (`Qt::WindowDoesNotAcceptFocus`).
- **Click-through.** `ignoresMouseEvents` is YES except while an unanswered card is up,
  so between confirmations every click lands on whatever is underneath.

The window is a `Qt::Tool` frameless panel. `MacWindow_mac.mm` changes what Qt gives it
(measured: level 8, MoveToActiveSpace, `hidesOnDeactivate` YES) to the following:

- `NSScreenSaverWindowLevel`, the same level as Electron's `"screen-saver"`;
- `CanJoinAllSpaces | FullScreenAuxiliary | Stationary | IgnoresCycle`;
- `hidesOnDeactivate` NO;
- no window shadow (the island draws its own).

These are applied once. Qt does not reset them on show.

The overlay appears top-center of the display under the mouse when a session starts, in
the display's work area, 12 px below the menu bar. It doesn't move during a session.

## Relation to the Electron overlay

| | Electron (`app/renderer/overlay/`) | Qt (`overlay-qt/`) |
|---|---|---|
| Messages | relayed by `main.js` over IPC | its own WebSocket, identified by `hello` |
| State rules | `overlay.js` | `OverlayModel`, a line-by-line port, unit tested |
| Look | `overlay.css` + `styles.css` | QML, same tokens, sizes and animations |
| Assets | `manifest.json` | the same file |
| Theme | main window's choice, else macOS | `--theme`, else macOS |
| Card answer | IPC to `main.js`, which sends `confirm` | sends `confirm` itself; the agent resolves it against the primary connection's pending confirmation and broadcasts `confirmation_resolved` with `"by": "overlay"` |

The overlay is never the agent's "primary" connection, so replies, the full four-way
confirmation card and skill proposals stay in the main window.

## Known limitations

- **WebM doesn't play with Homebrew's Qt.** Qt Multimedia decodes video through a backend
  plugin. Homebrew's `qtmultimedia` ships only the native one, AVFoundation
  (`libdarwinmediaplugin`), and Apple's framework has no WebM/VP9 support: its containers
  are avi, mp4, mov and some audio types. (Qt's own installer builds also include an FFmpeg
  backend, which does read WebM; this project doesn't depend on that.) A WebM state shows
  the orb, with a log line naming the file. The asset guide therefore recommends animated
  WebP, which is an *image* format (`AnimatedImage`, Qt's `qwebp` plugin) and needs no
  video backend at all. H.264 MP4 or MOV plays,
  but H.264 has no alpha. HEVC-with-alpha `.mov` should decode through AVFoundation but
  hasn't been tested. Animated GIF and WebP (via `AnimatedImage`), PNG and SVG all work.
  APNG shows its first frame only.
- **Theme choice.** The theme follows macOS, not the main window's in-app theme choice
  (that lives in Electron's `localStorage`). Electron can pass `--theme` if this matters.
- **Shadow under the island.** CSS `box-shadow` isn't painted under the element,
  `RectangularShadow` is. Behind the 94%-opaque light island that makes the background
  about 1% darker than Electron's (252 vs 255 per channel).
- **Fonts.** The code block uses the system fixed-pitch font from `QFontDatabase`; CSS
  `ui-monospace` resolves to SF Mono, which Qt can't request by name. Line heights
  are fixed to the CSS values, so layout matches to within about a pixel.
- **Argument order.** The card's argument summary is sorted by key: `QJsonObject` keeps
  keys sorted, while JavaScript keeps insertion order. One-argument calls are unaffected.
- **Video lifetime.** A video restarts from its first frame each time its state is
  re-entered: the item is recreated rather than kept mounted. This matches what a viewer
  sees in Electron, but means the file is reloaded each time.
- **Platforms.** It's macOS only in practice. The stub window layer builds elsewhere and
  toggles click-through with `Qt::WindowTransparentForInput`, but that path is untested.
- **Live appearance switching.** Following a macOS light/dark switch while running relies
  on `Application.styleHints` notifying. Only the forced themes and the startup
  appearance were exercised.
