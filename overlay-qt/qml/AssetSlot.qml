pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Effects
import QtMultimedia
import Leutheria.Core

// The square slot on the left of the island. Shows whatever the manifest maps
// the current state to: the built-in orb, an image, or a video. A file that
// fails at runtime is reported to the manifest, which swaps that state to
// the orb for the rest of the run (overlay.js's error handler does the same).
Item {
    id: slot

    required property string visualState
    required property real level
    required property AssetManifest manifest
    required property Theme theme

    readonly property var entry: manifest.entries[visualState] ?? { type: "css" }

    // CSS `transition: transform 0.09s linear` between the ~15 Hz level ticks.
    property real smoothLevel: level
    Behavior on smoothLevel { NumberAnimation { duration: 90 } }

    Loader {
        id: loader
        anchors.fill: parent
        sourceComponent: slot.entry.type === "video" ? videoAsset
                       : slot.entry.type !== "image" ? orbAsset
                       : /\.(gif|webp)$/i.test(slot.entry.source.toString()) ? animatedImage
                       : stillImage

        // Manifest "react": image/video assets follow the mic level too.
        scale: slot.entry.react === "scale" ? 1 + slot.smoothLevel * 0.18 : 1
        layer.enabled: slot.entry.react === "glow"
        layer.effect: MultiEffect {
            shadowEnabled: true
            shadowColor: slot.theme.ring
            shadowHorizontalOffset: 0
            shadowVerticalOffset: 0
            blurMax: 14
            shadowBlur: slot.smoothLevel
        }
    }

    Component {
        id: orbAsset
        Orb {
            visualState: slot.visualState
            level: slot.smoothLevel
            theme: slot.theme
        }
    }

    // AnimatedImage for the formats that animate (gif, webp); Image for the
    // rest (png, svg, ...). Neither animates APNG -- see README.
    Component {
        id: stillImage
        Image {
            source: slot.entry.source
            sourceSize: Qt.size(slot.width, slot.height) // SVG: rasterise at slot size (x DPR)
            fillMode: Image.PreserveAspectFit
            onStatusChanged: if (status === Image.Error) slot.manifest.markBroken(slot.visualState, "image error")
        }
    }

    Component {
        id: animatedImage
        AnimatedImage {
            source: slot.entry.source
            fillMode: Image.PreserveAspectFit
            onStatusChanged: if (status === Image.Error) slot.manifest.markBroken(slot.visualState, "image error")
        }
    }

    Component {
        id: videoAsset
        Video {
            source: slot.entry.source
            muted: true
            autoPlay: true
            loops: slot.entry.loop ? MediaPlayer.Infinite : 1
            fillMode: VideoOutput.PreserveAspectFit
            onErrorOccurred: (error, errorString) => slot.manifest.markBroken(slot.visualState, errorString)
        }
    }
}
