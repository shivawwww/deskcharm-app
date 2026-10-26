// Screen-records two walkthroughs of the static pages to web/video/ (webm -> mp4 via ffmpeg).
//   node scripts/video/record_demo.cjs [--leaflet <dir with leaflet.js/css>]
// --leaflet lets the CCC map run offline (tiles are not available then, so the map shows its grid and the markers only).
const { chromium } = require("playwright"); const fs = require("fs"), path = require("path"), { execFileSync } = require("child_process"), os = require("os");
const ROOT = path.resolve(__dirname, "..", ".."), OUT = path.join(ROOT, "web", "video");
const li = process.argv.indexOf("--leaflet"), LEAFLET = li > 0 ? process.argv[li + 1] : null, wait = (ms) => new Promise((r) => setTimeout(r, ms));
async function prepare(ctx) { await ctx.route("**/*.basemaps.cartocdn.com/**", (r) => r.abort()); await ctx.route("**/maps.googleapis.com/**", (r) => r.abort()); await ctx.route("**/maps.google.com/**", (r) => r.abort());
  if (LEAFLET) await ctx.route("**/cdnjs.cloudflare.com/ajax/libs/leaflet/**", (r) => r.fulfill({ path: path.join(LEAFLET, r.request().url().split("/").pop().replace(".min", "")) })); }
const scrollTo = (p, sel, ms = 1400) => p.evaluate(([s]) => document.querySelector(s).scrollIntoView({ behavior: "smooth", block: "start" }), [sel]).then(() => wait(ms));
async function record(name, fn) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "rec-")), b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1280, height: 720 }, recordVideo: { dir: tmp, size: { width: 1280, height: 720 } } }); await prepare(ctx); const p = await ctx.newPage();
  await fn(p); await wait(800); const vpath = await p.video().path(); await ctx.close(); await b.close();
  const mp4 = path.join(OUT, name + ".mp4"); execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-i", vpath, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "29", "-preset", "slow", "-r", "20", "-movflags", "+faststart", "-an", mp4]);
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-ss", "9", "-i", mp4, "-frames:v", "1", "-q:v", "4", path.join(OUT, name + ".jpg")]);
  fs.rmSync(tmp, { recursive: true, force: true }); console.log("wrote", name + ".mp4", (fs.statSync(mp4).size / 1024) | 0, "KB");
}
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  await record("demo_ccc_simulation", async (p) => {          // real time, no cuts
    await p.goto("file://" + path.join(ROOT, "web", "ccc.html") + "?map=osm"); await wait(6500);
    await p.click("#queue .q >> nth=0"); await wait(5000);
    await p.selectOption("#scSel", "ar_gulf_fire"); await p.click("#spawn"); await wait(7000);
    await p.click("#auto"); await wait(5500);                    // switch to manual dispatch
    const btn = p.locator("#detail [data-d]").first(); if (await btn.count()) { await btn.click(); await wait(4000); }
    await p.click("#auto"); await p.click("#surge"); await wait(7000);
    await p.click("#queue .q >> nth=2"); await wait(5000); await p.click("#speeds [data-s='2']"); await wait(9000);
  });
  await record("demo_usecases_tour", async (p) => {
    await p.goto("file://" + path.join(ROOT, "web", "usecases.html")); await wait(2500);
    await scrollTo(p, "#how", 2400); await scrollTo(p, "#usecases", 2400);
    await p.click("#langbar [data-l=ar]"); await wait(2500); await p.click("#langbar [data-l=ta]"); await wait(2200); await p.click("#langbar [data-l=en]"); await wait(1500);
    await p.click("#grid .card[data-id=cardiac]"); await wait(3200); await p.click("#dDemo"); await wait(9000);
    await scrollTo(p, "#audio", 3000); await scrollTo(p, "#map", 2800);
    await p.click("#hlpBtn"); await wait(800); await p.fill("#hlpQ", "is dispatch real"); await p.press("#hlpQ", "Enter"); await wait(3500); await p.click("#hlpX");
    await scrollTo(p, "#integrations", 3000); await scrollTo(p, "#deploy", 2500);
  });
})();
