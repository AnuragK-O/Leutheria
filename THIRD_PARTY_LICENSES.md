# Third-party licences

Everything a Leutheria build ships, downloads at run time, or links against, with its
licence, where it comes from, and what distributing it obliges us to do. The package
tables are generated from installed metadata (`cd agent && ./.venv/bin/python
scripts/licenses_report.py --write`, after `npm install` in `app/`); everything else here is
hand-written. "Verify" marks a licence that could not be confirmed from a file on this
machine and must be checked against its source before a release.

Last reviewed 2026-10-15. This is an engineering inventory, not legal advice.

## Decisions needed

1. **openWakeWord's pretrained wake-word models are non-commercial.** The `hey_jarvis` model
   (the default wake word) and the other pretrained ones are published under CC BY-NC-SA 4.0
   (openWakeWord README; verify against the release you download). Fine for a free,
   non-commercial release with attribution, if the model is downloaded at run time rather
   than redistributed under different terms; **not usable in any commercial distribution**.
   Options: train our own wake word (openWakeWord's training notebook; the resulting model is
   ours) and ship that, or keep the download-at-first-use flow and stay non-commercial.
2. **ffmpeg (push-to-talk decoding).** Whisper shells out to `ffmpeg` to decode the
   push-to-talk recording (`stt.transcribe(path)`); the voice session passes arrays and
   never touches it. Homebrew's ffmpeg is GPL-3.0-or-later. Today the user installs it, so
   nothing is distributed; bundling it would make the bundle GPL. Options: decode the WAV
   the agent itself wrote in Python and drop the dependency (preferred, small change), or
   ship an LGPL-only ffmpeg build as a separate dynamically invoked binary with its source offer.
3. **Piper** is now optional and unshipped (`agent/requirements-piper.txt`, GPL-3.0, wheel
   bundles espeak-ng, also GPL-3.0). Nothing to decide unless someone wants it bundled
   again; a user installing it themselves is not distribution by us.
4. **Qt for the native overlay** (off by default) must be shipped as dynamically linked
   frameworks under LGPL-3.0 with the obligations below. Static linking or modifying Qt
   without publishing the changes would need a commercial Qt licence. Decide whether the
   Qt overlay ships at all in the first release.
5. **Electron's `@electron/get` tree is install-time only.** It downloads the Electron
   binary during `npm install`; a packaged app ships `Electron.app` (Chromium et al.) and
   `ws`, not the downloader. Fine either way (all permissive), but the packager should
   include `LICENSES.chromium.html`.
6. **certifi and tqdm are MPL-2.0** (file-level copyleft): fine for closed distribution as
   long as their files ship unmodified (or modified files are published) and the licence
   notice is included.

Nothing else in the shipped set is copyleft; the remaining packages are MIT, BSD, Apache-2.0,
ISC, PSF or similar, whose obligation is to include the copyright notice and licence text.

## Downloaded at run time (models)

| What | Where it goes | Licence | Source | Obligation |
|---|---|---|---|---|
| Whisper `base` weights (`base.pt`) | `~/.cache/whisper` | MIT (code and weights) | https://github.com/openai/whisper | Notice. |
| openWakeWord feature models (`melspectrogram`, `embedding_model`) | the `openwakeword` package's `resources/models` | Apache-2.0 (derived from Google's speech_embedding model) -- verify | https://github.com/dscripka/openWakeWord | Notice; state changes. |
| openWakeWord VAD (`silero_vad.onnx`) | same | MIT (Silero VAD) -- verify | https://github.com/snakers4/silero-vad | Notice. |
| openWakeWord pretrained wake words (`hey_jarvis_v0.1` default, others selectable) | same | **CC BY-NC-SA 4.0** -- verify | https://github.com/dscripka/openWakeWord#pre-trained-models | Attribution, non-commercial only, share-alike. See Decisions 1. |
| Piper voices (`en_US-amy-medium` default) -- only if Piper is installed | `agent/agent/assets/voices/` | per voice; the `amy` dataset licence is stated in its model card -- verify | https://huggingface.co/rhasspy/piper-voices | Per voice; not shipped by us. |

## Bundled fonts

| Font | Files | Licence | Source | Obligation |
|---|---|---|---|---|
| Chakra Petch | `app/renderer/fonts/ChakraPetch-{400,500,600,700}.woff2` (also compiled into the Qt overlay's resources) | SIL OFL 1.1 (`ChakraPetch-OFL.txt`) | https://github.com/m4rc1e/Chakra-Petch | Ship the OFL text with the fonts; don't sell the fonts alone; no Reserved Font Name on modified versions. |
| JetBrains Mono | `app/renderer/fonts/JetBrainsMono-{Regular,Medium,Bold}.woff2` (same) | SIL OFL 1.1 (`JetBrainsMono-OFL.txt`) | https://github.com/JetBrains/JetBrainsMono | Same. |

## Native overlay (`overlay-qt/`, optional)

| Component | Licence | Source | Obligation |
|---|---|---|---|
| Qt 6 (Core, Gui, Qml, Quick, WebSockets, Multimedia; Homebrew build) | LGPL-3.0 (open-source option; GPL-2.0/3.0 also offered) | https://www.qt.io/licensing, https://download.qt.io | Link dynamically (the app bundle's Frameworks, replaceable by the user); ship the LGPL-3.0 and GPL-3.0 texts and a Qt notice; offer the Qt source used (or point to the exact release); don't forbid reverse engineering for debugging those modifications. Check each module's third-party notices (Qt Multimedia's AVFoundation backend uses system frameworks only). |

## Electron runtime

| Component | Licence | Source | Obligation |
|---|---|---|---|
| Electron 44 (bundles Chromium, Node.js, V8, and a `libffmpeg` built without proprietary codecs) | MIT; Chromium's components are listed in `node_modules/electron/dist/LICENSES.chromium.html` (BSD, MIT, Apache, LGPL for ffmpeg, ...) | https://github.com/electron/electron | Ship `LICENSE` and `LICENSES.chromium.html` inside the app; the bundled ffmpeg is LGPL-2.1 and dynamically linked, as Electron ships it. |

## System components used, not shipped

macOS `say` and its voices (the local TTS fallback), Python 3.12 (PSF licence; ships only if
a packaged build bundles an interpreter, then its licence text goes in too). Cloud services (Anthropic, ElevenLabs) are governed
by their terms of service, not a licence; their keys are the user's own.

## Packages

Native libraries inside the wheels travel with them and carry their own notices: PortAudio
in `sounddevice` (MIT), OpenBLAS and the GCC Fortran runtime in `numpy`/`scipy` (BSD; the
runtime is GPL-3.0 *with* the GCC Runtime Library Exception, which permits closed
distribution), LLVM in `llvmlite` (Apache-2.0 with LLVM exception). Keep each wheel's
`*.dist-info/licenses` files in a packaged build.

<!-- BEGIN GENERATED: licenses_report.py -->

### Python packages shipped (agent/requirements.txt and dependencies: 58)

| Package | Version | Licence | Source |
|---|---|---|---|
| annotated-types | 0.8.0 | MIT | https://github.com/annotated-types/annotated-types |
| anthropic | 1.2.0 | MIT | https://github.com/anthropics/anthropic-sdk-python |
| anyio | 4.14.2 | MIT | https://github.com/agronholm/anyio |
| certifi | 2026.7.22 | MPL-2.0 | https://github.com/certifi/python-certifi |
| cffi | 2.1.1 | MIT-0 | https://github.com/python-cffi/cffi/releases |
| charset-normalizer | 3.5.1 | MIT | https://github.com/jawah/charset_normalizer/blob/master/CHANGELOG.md |
| cloudpickle | 3.1.2 | BSD-3-Clause | https://github.com/cloudpipe/cloudpickle |
| docstring_parser | 0.18.0 | MIT | https://github.com/rr-/docstring_parser |
| filelock | 3.32.5 | MIT | https://github.com/tox-dev/py-filelock |
| flatbuffers | 25.12.19 | Apache 2.0 | https://github.com/google/flatbuffers |
| fsspec | 2026.7.0 | BSD-3-Clause | https://github.com/fsspec/filesystem_spec |
| h11 | 0.16.0 | MIT | https://github.com/python-hyper/h11 |
| httpcore2 | 2.12.0 | BSD-3-Clause | https://github.com/pydantic/httpx2/blob/main/src/httpcore2/CHANGELOG.md |
| httpx2 | 2.12.0 | BSD-3-Clause | https://github.com/pydantic/httpx2/blob/main/src/httpx2/CHANGELOG.md |
| idna | 3.19 | BSD-3-Clause | https://github.com/kjd/idna/blob/master/HISTORY.md |
| Jinja2 | 3.1.6 | BSD License | https://github.com/pallets/jinja/ |
| jiter | 0.16.0 | MIT | https://github.com/pydantic/jiter/ |
| joblib | 1.6.0 | BSD-3-Clause | https://github.com/joblib/joblib |
| llvmlite | 0.49.0 | BSD-2-Clause AND Apache-2.0 WITH LLVM-exception | https://github.com/numba/llvmlite |
| MarkupSafe | 3.0.3 | BSD-3-Clause | https://github.com/pallets/markupsafe/ |
| more-itertools | 11.1.0 | MIT | https://github.com/more-itertools/more-itertools |
| mpmath | 1.3.0 | BSD | https://github.com/fredrik-johansson/mpmath |
| narwhals | 2.26.0 | MIT | https://github.com/narwhals-dev/narwhals |
| networkx | 3.6.1 | BSD-3-Clause | https://github.com/networkx/networkx/issues |
| numba | 0.67.0 | BSD | https://numba.pydata.org |
| numpy | 2.5.2 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | https://github.com/numpy/numpy |
| onnxruntime | 1.29.0 | MIT License | https://onnxruntime.ai |
| openai-whisper | 20250625 | MIT | https://github.com/openai/whisper |
| openwakeword | 0.6.0 | Apache Software License | https://github.com/dscripka/openWakeWord |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | https://github.com/pypa/packaging |
| protobuf | 7.36.1 | 3-Clause BSD License | https://developers.google.com/protocol-buffers/ |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| pydantic | 2.13.5 | MIT | https://github.com/pydantic/pydantic |
| pydantic_core | 2.46.5 | MIT | https://github.com/sponsors/samuelcolvin |
| pyobjc-core | 12.2.2 | MIT | https://github.com/ronaldoussoren/pyobjc/issues |
| pyobjc-framework-ApplicationServices | 12.2.2 | MIT | https://github.com/ronaldoussoren/pyobjc/issues |
| pyobjc-framework-Cocoa | 12.2.2 | MIT | https://github.com/ronaldoussoren/pyobjc/issues |
| pyobjc-framework-CoreText | 12.2.2 | MIT | https://github.com/ronaldoussoren/pyobjc/issues |
| pyobjc-framework-Quartz | 12.2.2 | MIT | https://github.com/ronaldoussoren/pyobjc/issues |
| python-dotenv | 1.2.3 | BSD-3-Clause | https://github.com/theskumar/python-dotenv |
| regex | 2026.9.3 | Apache-2.0 AND CNRI-Python | https://github.com/mrabarnett/mrab-regex |
| requests | 2.34.2 | Apache-2.0 | https://github.com/psf/requests |
| scikit-learn | 1.9.1 | BSD-3-Clause | https://github.com/scikit-learn/scikit-learn |
| scipy | 1.18.1 | BSD License | https://github.com/scipy/scipy |
| Send2Trash | 2.1.0 | BSD-3-Clause | https://github.com/arsenetar/send2trash |
| setuptools | 84.0.0 | MIT | https://github.com/pypa/setuptools |
| sniffio | 1.3.1 | MIT OR Apache-2.0 | https://github.com/python-trio/sniffio |
| sounddevice | 0.5.6 | MIT | https://github.com/spatialaudio/python-sounddevice/ |
| sympy | 1.14.0 | BSD | https://github.com/sympy/sympy |
| threadpoolctl | 3.7.0 | BSD-3-Clause | https://github.com/joblib/threadpoolctl |
| tiktoken | 0.14.0 | MIT License | https://github.com/openai/tiktoken |
| torch | 2.13.0 | Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause AND BSD-3-Clause AND BSL-1.0 AND MIT | https://github.com/pytorch/pytorch |
| tqdm | 4.70.0 | MPL-2.0 AND MIT | https://github.com/tqdm/tqdm |
| truststore | 0.10.4 | MIT | https://github.com/sethmlarson/truststore |
| typing-inspection | 0.4.4 | MIT | https://github.com/pydantic/typing-inspection |
| typing_extensions | 4.16.0 | PSF-2.0 | https://github.com/python/typing_extensions/issues |
| urllib3 | 2.7.0 | MIT | https://github.com/urllib3/urllib3/blob/main/CHANGES.rst |
| websockets | 17.1 | BSD-3-Clause | https://github.com/python-websockets/websockets |

### Python packages NOT shipped (agent/requirements-piper.txt, optional, user-installed)

| Package | Version | Licence | Source |
|---|---|---|---|
| pathvalidate | 3.3.1 | MIT License | https://github.com/thombashi/pathvalidate/blob/master/CHANGELOG.md |
| piper-tts | 1.7.0 | GPL-3.0-or-later | http://github.com/OHF-voice/piper1-gpl |

### npm packages shipped (app/package.json dependencies and their dependencies: 13)

| Package | Version | Licence | Source |
|---|---|---|---|
| @electron-internal/extract-zip | 1.0.5 | BSD-2-Clause | https://github.com/electron/extract-zip |
| @electron/get | 5.1.0 | MIT | https://github.com/electron/get |
| @types/node | 24.13.3 | MIT | https://github.com/DefinitelyTyped/DefinitelyTyped |
| debug | 4.4.3 | MIT | https://github.com/debug-js/debug |
| electron | 44.0.0 | MIT | https://github.com/electron/electron |
| env-paths | 3.0.0 | MIT | https://github.com/sindresorhus/env-paths |
| graceful-fs | 4.2.11 | ISC | https://github.com/isaacs/node-graceful-fs |
| ms | 2.1.3 | MIT | https://github.com/vercel/ms |
| progress | 2.0.3 | MIT | https://github.com/visionmedia/node-progress |
| semver | 7.8.5 | ISC | https://github.com/npm/node-semver |
| sumchecker | 3.0.1 | Apache-2.0 | https://github.com/malept/sumchecker |
| undici-types | 7.18.2 | MIT | https://github.com/nodejs/undici |
| ws | 8.21.3 | MIT | https://github.com/websockets/ws |

<!-- END GENERATED -->
