# Overlay assets

This folder is where the activation overlay's visuals live. The overlay is the small
floating "island" that appears at the top-center of the screen while a voice session is
running: a square **asset slot** on the left, a state label and one line of text on the
right, and (only when Leutheria needs an approval) a compact card underneath.

Everything in the slot comes from `manifest.json`. Dropping in a new asset is a file copy
plus a manifest edit: no code changes, no build step.

## The states

| state | when it shows | typical length |
|---|---|---|
| `listening` | Session is open and the mic is live, waiting for you to talk. Also where it returns after every reply. | seconds to minutes |
| `hearing` | Speech detected; you're mid-sentence. | a few seconds |
| `transcribing` | You stopped talking; Whisper is turning it into text. | ~0.5 to 2 s |
| `thinking` | The command is running (model + tools). | 1 to 20 s |
| `speaking` | Leutheria is saying its reply out loud. | the length of the reply |
| `awaiting_confirmation` | A risky action needs a yes/no. The confirmation card is shown under the island and the mic stays live for a spoken answer. | until answered |

The overlay fades in when a session starts (wake word, the tray's "Start listening", or
Alt+Space) and fades out when it ends. There is no asset for "off" or "idle": the overlay
isn't on screen then.

## manifest.json

```json
{
  "size": 56,
  "states": {
    "listening":  { "type": "image", "src": "listening.webp", "react": "scale" },
    "hearing":    { "type": "image", "src": "hearing.webp" },
    "thinking":   { "type": "image", "src": "thinking.svg" },
    "speaking":   { "type": "css" }
  }
}
```

| field | meaning |
|---|---|
| `size` | Side of the square slot in CSS pixels. Default 56, allowed 32 to 120. The island grows to fit. |
| `type` | `"css"` (the built-in placeholder orb), `"image"` (png, gif, svg, apng, webp), or `"video"` (webm, mp4). |
| `src` | File name, relative to this folder. |
| `loop` | Videos only. Default `true`; set `false` for a one-shot that should hold its last frame. |
| `react` | Optional. How the frame reacts to the mic level for image/video assets: `"scale"` (grows up to 18%), `"glow"` (a violet halo), or `"none"` (default; use this if your asset animates itself). |

A state missing from the manifest, a typo in `type`, or a file that doesn't exist falls back
to the CSS placeholder for that state, with a line in the Logs view saying why. You can
replace one state at a time.

## Formats and sizes

- **Transparency is required.** The overlay window is transparent and sits over whatever
  the user has open. Draw on a transparent background; the island behind the slot is dark
  violet in dark mode and near-white in light mode, so avoid relying on either.
- **Animated: use animated WebP** (`"type": "image"`). It has real (8-bit) transparency,
  loops, stays small, and is the one animated format that plays in **both** overlays — the
  Electron one and the native Qt one (`overlay-qt/`). Export your animation as a PNG
  sequence with alpha, then pack it with `img2webp` (from `brew install webp`):

  ```bash
  # from a video with an alpha channel (skip if you already have PNG frames)
  mkdir frames && ffmpeg -i in.mov frames/%04d.png
  # -d is milliseconds per frame (33 = 30 fps); -loop 0 loops forever
  img2webp -loop 0 -d 33 -lossy -q 85 frames/*.png -o listening.webp
  ```

  Check it with `webpmux -info listening.webp`: it should say
  `Features present: animation transparency`. (Homebrew's ffmpeg has no WebP encoder, so
  `ffmpeg ... out.webp` won't work; use `img2webp`.) Most motion tools (After Effects,
  Rive, Figma plugins) can also export animated WebP directly.
- **Formats to avoid for animation:** transparent WebM (VP9 alpha) plays in the Electron
  overlay but *not* in the Qt one, which shows the placeholder orb instead (see
  `overlay-qt/README.md`). Animated GIF plays in both but has 1-bit transparency (jagged
  edges on the island). APNG animates in Electron only. mp4 has no alpha.
- **Static or vector:** SVG (scales perfectly, and may contain its own CSS animation) or PNG.
- **Resolution:** at least 4x the slot size for Retina, so **224 x 224 px** for the default
  56 px slot. Square canvas; the asset is scaled to fit (`object-fit: contain`).
- **Loops:** `listening`, `hearing`, `thinking`, `speaking` and `awaiting_confirmation` can
  last indefinitely, so make them seamless loops. `transcribing` is short; a loop is still
  safest.
- Videos play muted and are never reloaded on revisit, so a state you return to picks up
  at the start of its loop.

## The `--level` property

While the mic is live (`listening`, `hearing`, `awaiting_confirmation`) the agent sends the
smoothed mic level about 15 times a second, and the overlay puts it on the page as the CSS
custom property `--level`, a number from 0 (silence) to 1 (loud). It's 0 in every other state.

- For image/video assets, `"react": "scale"` or `"glow"` in the manifest is the easy way to
  use it.
- The CSS placeholder uses it directly (see `.orb` in `../overlay.css`), e.g.
  `transform: scale(calc(0.92 + var(--level) * 0.22))`. If you'd rather hand-build a state
  in CSS than export a file, edit the matching `.island[data-state="..."] .orb` block there
  and keep `"type": "css"` in the manifest.
- An SVG shown through `<img>` can't see page CSS variables, so it can't read `--level`
  itself; use `react` for those.

## Previewing

The quickest loop is the scripted mock agent, which walks through every state (including a
confirmation) without needing the Python side, a microphone, or an API key:

```bash
cd app
node scripts/mock-agent.js &          # plays a scripted session on port 8799
env -u ELECTRON_RUN_AS_NODE LEUTHERIA_AGENT_URL=ws://127.0.0.1:8799 LEUTHERIA_NO_SPAWN=1 npx electron .
```

The mock starts a session a couple of seconds after the app connects and again whenever you
press Alt+Space or pick "Start listening" from the menu bar icon. Manifest changes are read
each time the overlay loads, so restart the app (or the mock) after editing it.

To get still frames of every state instead of watching live, add
`LEUTHERIA_CAPTURE_DIR=/some/folder` to the Electron command: a PNG of the overlay (and the
main window) is written on every state change. Add `LEUTHERIA_CAPTURE_THEME=light` or
`dark` to force the overlay's appearance for that run.

Light or dark: the overlay follows the theme chosen in the main window (the button at the
bottom of its sidebar), and the macOS appearance if none has been chosen.
