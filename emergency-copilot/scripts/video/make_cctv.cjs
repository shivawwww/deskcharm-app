// Renders the four simulated CCTV clips to web/video/cctv_<scene>.mp4 (+ poster jpg).
//   node scripts/video/make_cctv.cjs            render all clips
//   node scripts/video/make_cctv.cjs --preview  write a contact sheet to the given dir (arg 2) to check the look
// Needs: playwright (node), ffmpeg with libx264. Output is committed, CI does not run this.
const { chromium } = require("playwright"); const fs = require("fs"), path = require("path"), { execFileSync } = require("child_process"), os = require("os");
const ROOT = path.resolve(__dirname, "..", ".."), OUT = path.join(ROOT, "web", "video"), FPS = 24, DUR = 12;
(async () => {
  const preview = process.argv[2] === "--preview"; fs.mkdirSync(OUT, { recursive: true });
  const b = await chromium.launch(); const p = await b.newPage({ viewport: { width: 640, height: 360 } });
  await p.goto("file://" + path.join(__dirname, "cctv.html")); const scenes = await p.evaluate(() => window.SCENE_NAMES);
  for (const sc of scenes) {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cctv-")), times = preview ? [1.5, 3.5, 4.6, 6.5, 8.5, 10.5] : Array.from({ length: FPS * DUR }, (_, i) => i / FPS);
    for (let i = 0; i < times.length; i++) { const b64 = await p.evaluate(([s, t, f]) => window.renderFrame(s, t, f), [sc, times[i], preview ? Math.round(times[i] * FPS) : i]); fs.writeFileSync(path.join(tmp, `f${String(i).padStart(4, "0")}.jpg`), Buffer.from(b64, "base64")); }
    if (preview) { execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", path.join(tmp, "f%04d.jpg"), "-vf", "tile=3x2", "-frames:v", "1", path.join(process.argv[3], `sheet_${sc}.jpg`)]); }
    else {
      execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", String(FPS), "-i", path.join(tmp, "f%04d.jpg"), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "27", "-preset", "slow", "-movflags", "+faststart", "-an", path.join(OUT, `cctv_${sc}.mp4`)]);
      fs.copyFileSync(path.join(tmp, `f${String(Math.round(5.5 * FPS)).padStart(4, "0")}.jpg`), path.join(OUT, `cctv_${sc}.jpg`)); console.log("wrote", `cctv_${sc}.mp4`, (fs.statSync(path.join(OUT, `cctv_${sc}.mp4`)).size / 1024) | 0, "KB");
    }
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  await b.close();
})();
