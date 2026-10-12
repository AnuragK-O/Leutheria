/* Regenerates app/renderer/img/mark-*.png from assets/1.png.

     cd app && env -u ELECTRON_RUN_AS_NODE npx electron scripts/make-mark.js

   The source logo is an opaque 2000 px RGB PNG: brand violet drawn on white.
   The UI needs it small, and on a transparent ground so it can sit on the dark
   base and be used as a CSS mask (a tintable single-colour mark). So each
   output is the logo's violet shape only -- flat brand violet, with the alpha
   channel carrying the shape and its anti-aliasing -- and the white is gone.

   Run with Electron rather than node because it needs an image decoder and
   the project has no image library; nativeImage is already there. */

const { app, nativeImage } = require("electron");
const fs = require("fs");
const path = require("path");

const SOURCE = path.join(__dirname, "..", "..", "assets", "1.png");
const OUT_DIR = path.join(__dirname, "..", "renderer", "img");
const SIZES = [64, 128, 256, 512];
const VIOLET = { r: 142, g: 111, b: 212 }; // the logo's own violet, #8E6FD4

app.whenReady().then(() => {
  const source = nativeImage.createFromPath(SOURCE);
  if (source.isEmpty()) {
    console.error(`make-mark: couldn't read ${SOURCE}`);
    app.exit(1);
    return;
  }
  fs.mkdirSync(OUT_DIR, { recursive: true });

  for (const size of SIZES) {
    const small = source.resize({ width: size, height: size, quality: "best" });
    const pixels = small.toBitmap(); // BGRA
    const { width, height } = small.getSize();
    const out = Buffer.alloc(pixels.length);
    for (let i = 0; i < pixels.length; i += 4) {
      // A pixel is white mixed with violet by coverage; green separates them
      // furthest (255 vs 111), so it gives the cleanest coverage estimate.
      const coverage = Math.max(0, Math.min(1, (250 - pixels[i + 1]) / (250 - VIOLET.g)));
      // Premultiplied, which is what createFromBitmap expects.
      out[i] = Math.round(VIOLET.b * coverage);
      out[i + 1] = Math.round(VIOLET.g * coverage);
      out[i + 2] = Math.round(VIOLET.r * coverage);
      out[i + 3] = Math.round(255 * coverage);
    }
    const file = path.join(OUT_DIR, `mark-${size}.png`);
    fs.writeFileSync(file, nativeImage.createFromBitmap(out, { width, height }).toPNG());
    console.log(`${file} ${fs.statSync(file).size} bytes`);
  }
  app.exit(0);
});
