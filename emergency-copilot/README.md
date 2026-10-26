# Emergency Call Co-Pilot: MVP

Reference implementation of the Phase 1 (MVP) slice of the *AI Emergency Call Co-Pilot* concept: a real-time layer that
listens alongside the call-taker, extracts structured incident data, raises critical cues, guides the next SOP question and
pre-fills the CAD record. **The AI only proposes; the call-taker accepts, edits or rejects every field.**

Runs on a laptop with no GPU. The speech and language models are pluggable; this MVP ships deterministic baselines
(lexicon/regex extractor, fuzzy geocoder, rules engine) behind the same interfaces the production models will use.

```bash
pip install -r requirements.txt
python -m copilot            # http://127.0.0.1:8080  (HOST / PORT env vars)
python -m pytest             # 69 tests
python -m copilot.eval       # replay scenarios, score against MVP KPI targets
```

Open the UI, pick a scenario (English, Gulf Arabic + English code-switch, Levantine, Egyptian), press **Start simulated call**.
You can also type caller lines, or use **Mic** (Chrome Web Speech API) for live speech.

## Architecture (mirrors the HLD layers)

```
 audio / text ──► ASR gateway ──► event bus ──► call orchestrator ──► call-taker panel (WebSocket)
 (scripted | browser | Whisper)    (bus.py)      (orchestrator.py)     supervisor wall, admin, audit
                                                   │  ▲
                       extractor · location · reasoner · SOP engine ── audit log ── CAD adapter
```

| Module | HLD component | Catalogue features (P1) |
|---|---|---|
| `speech.py`, `POST /api/calls/{id}/segments`, `/ws/audio` | Media gateway + Speech AI interface | 1, 3, 4*, 6 |
| `lang.py` | Language / dialect ID | 2, 3 |
| `extractor.py` | Fast extractor | 9, 11, 12 |
| `location.py` | Location engine (gazetteer fuzzy + Latin phonetic skeleton + AML fusion) | 17, 19, 21 |
| `reasoner.py` | Severity (explained) and summary | 10, 25 |
| `sop.py` + `data/sop.json` | Deterministic SOP engine, self-ticking checklist | 22, 23 |
| `orchestrator.py` | Call orchestrator, thresholds, human-in-the-loop | 28 |
| `cad.py` | CAD adapters (`MockCAD` writes `var/cad/*.json`, `RestCAD`) | 28 |
| `audit.py` | HMAC hash-chained audit log | 47 |
| `web/` | Call-taker panel, supervisor wall, admin | — |

\* Speaker labels come from the channel (caller / call-taker) rather than diarisation of a mixed channel.

Governance built in: shadow mode (AI computed and logged, hidden from call-takers, nothing written to CAD) with a scoring
endpoint `GET /api/shadow/report` against the call-taker's own entries (`POST /api/calls/{id}/truth`); per-feature kill
switches (`/api/admin/features/{name}`); every transcript line, suggestion, cue, accept/reject/edit and CAD write in the audit
chain (`GET /api/audit/verify` detects any edit, deletion or reorder); call archive with call-taker corrections in
`var/calls/*.json` as the "collect" step of the learning loop.

## Abu Dhabi regions and case record

The gazetteer (`data/gazetteer.json`) covers the three regions of the Emirate: **Abu Dhabi Region** (island, Reem / Maryah,
Saadiyat / Yas, Mussafah, mainland), **Al Ain Region** (city, Jebel Hafeet) and **Al Dhafra Region** (Madinat Zayed, Ruwais,
Liwa, Ghayathi, Al Mirfa). Every place carries `region` and `area`; a handset-only position is mapped to the nearest known place.
Each call gets a **case record** (`casefile.py`, `GET /api/calls/{id}/case`, shown in the call-taker panel):

* case number `AD-<year>-<seq>` (sequence survives restarts), status, caller and language
* region (English / Arabic), area, location with confidence and acceptance status, coordinates, building / floor
* incident type and code, priority, people / injuries / hazards, critical cues
* responding agencies with hotlines and the **nearest hospital / police / civil defence facility** (straight-line km, not ETA)
* data-quality flags (low-confidence location, handset-only position, unacknowledged cues) and a timeline of AI and human events

Reference data (`regions.json`, `facilities.json`, `agencies.json`) is demo data with approximate coordinates. In shadow mode the
case record exposes only the non-AI parts. Unit recommendation and ETA remain Phase 2.

## Use-case showcase page

`web/usecases.html` is a self-contained static page (no server, no CDN, inline icons and illustrations, light/dark, responsive)
for demos and bids: 25 use cases (17 built in the MVP, 8 roadmap), each with its own illustrated scene (`web/art.js`), filters and a detail dialog,
switchable between **English, Arabic (right-to-left), Hindi, Tamil and Malayalam** (`copilot/data/usecases.json`; the non-English text is
machine-drafted and needs native-speaker review; the rest of the page stays English), plus sections for integrations (with built vs planned status),
deployment, security and compliance intent, GPU sizing, commercial options, FAQ, a replayable simulated call,
a schematic Abu Dhabi region map, roles, safety model, KPI targets and roadmap. The demo frames come from the real pipeline.
It also has five-language audio samples (English, Arabic, Hindi, Tamil, Malayalam: synthetic voices, clips in `web/audio/`,
text in `copilot/data/audio_samples.json`, regenerate with `python scripts/build_audio.py`, needs espeak-ng and ffmpeg) and a Google Maps
embed (the only thing it loads from the internet). Hindi, Tamil and Malayalam are audio and text only: the Co-Pilot does not understand
them yet, and the page shows that using the real extractor output.
Regenerate after changing scenarios or data with `python scripts/build_usecases.py` (template: `web/usecases.template.html`).
Also served by the app at `/usecases.html`.

## Full-view CCC simulation

`web/ccc.html` is a self-contained, full-screen Command and Control Centre simulation: incident queue, live map of the emirate
(Leaflet, dark CARTO tiles), unit list, event log, and for the selected incident the replayed call, AI alerts, extracted fields,
**real-time SOP** (next question, self-ticking checklist, pre-arrival instructions) and **dispatch**. Calls are replayed from the real
extraction pipeline; **dispatch, units and their movement are simulated** (unit recommendation is Phase 2 and not built in the product).
Controls: pause, 1x/2x/4x time-lapse, autopilot (simulated call-taker and dispatcher) or manual Dispatch buttons, random incoming calls,
spawn a call, **Surge x6** (units run out, requests queue), reset, full screen. ETAs are straight-line x1.3 at 60 km/h.

**Map provider:** OpenStreetMap/CARTO (free, no key) by default. Google Maps is available through **Map settings** on the page (or `?gmapsKey=...&map=google`): there is no keyless Google map API, so you supply a Maps JavaScript API key, restricted to your site's HTTP referrer. It falls back to OpenStreetMap if the key is missing or Google fails. The Google path is covered by a test with a stand-in for the Google library, not against live Google.

## Optional AI services (all OFF by default)

Nothing is sent to any AI service unless an operator turns it on. Every AI request is recorded in the audit log.

| Service | Enable | What it does |
|---|---|---|
| **Support agent** (Claude) | `COPILOT_LLM=anthropic`, `ANTHROPIC_API_KEY`, optional `COPILOT_LLM_MODEL` (default `claude-opus-5-5`) | Chat panel in the app (`/api/agent/chat`). **Read-only** tools: help search, protocol questions, system and audit status. Cannot dispatch, edit or accept anything, gives no medical advice. Uses the server-side refusal fallback. Without a key it falls back to keyword help |
| Call-aware agent and **AI draft summary** | additionally `COPILOT_LLM_SEND_CALL_DATA=1` | Lets the agent explain a call's priority and cues, and drafts a narrative field (`/api/calls/{id}/ai-summary`). Separate opt-in because it sends call content to the provider. Priority stays rule-based |
| **Local LLM (Ollama)** | install [Ollama](https://ollama.com), `ollama pull llama3.1`, then `COPILOT_LLM=ollama` (defaults: `http://localhost:11434/v1`, model `llama3.1`; override with `COPILOT_LLM_MODEL` / `COPILOT_LLM_BASE_URL`) | The same read-only agent and draft summary on a model that runs on your own machine, so no call data leaves it. Uses tool calling when the model supports it; otherwise (or with `COPILOT_LLM_TOOLS=0`) it answers from the help base |
| Any OpenAI-compatible LLM | `COPILOT_LLM=openai_compat`, `COPILOT_LLM_BASE_URL`, `COPILOT_LLM_MODEL`, optional `COPILOT_LLM_API_KEY` | Same as above for vLLM, llama.cpp server or another compatible server |
| **Whisper speech recognition** (local) | `pip install faster-whisper`, `COPILOT_ASR=whisper`, optional `COPILOT_WHISPER_MODEL` / `COPILOT_WHISPER_DEVICE` | Powers `/ws/audio/{call_id}` and `POST /api/transcribe` (raw audio body, `?language=ar`) to check real recordings. Runs on your machine; Arabic, English, Hindi, Tamil and Malayalam are supported by Whisper, but the Co-Pilot's extraction still understands only Arabic and English |

The AI integrations are covered by tests that use fake providers; they have **not** been run against the live Claude API or a real Whisper
model in this repository's CI (no key or model available there). The static pages (`usecases.html`, `ccc.html`) carry an **AI agent** button (robot icon). By default it is keyword search that sends
nothing anywhere; if Ollama is running on the visitor's own computer (started with `OLLAMA_ORIGINS` allowing the page), the page detects it
and answers with that local model, sending the question only to `localhost`.

## Video samples (`web/video/`)

Six MP4 files, about 4 MB in total, no audio, referenced by `usecases.html` and `ccc.html`:

| File | What it is |
|---|---|
| `demo_ccc_simulation.mp4` | Screen recording (52 s, real time) of the CCC simulation: calls, alerts, SOP, manual and autopilot dispatch, a surge, units moving |
| `demo_usecases_tour.mp4` | Screen recording (43 s) of the use-case page: cards, languages, dialog, call replay, regions, AI agent |
| `cctv_collision / fire / crime / medical.mp4` | Four 12 s **simulated** CCTV clips, drawn as canvas animations (`scripts/video/cctv.html`). They are **not real footage** and say so in the frame. The CCC plays the matching clip in the incident panel; real camera fusion (ONVIF, ANPR) is a Phase 2 feature and is not built |

I did not find or use any real incident footage: none could be downloaded from the build environment, and real footage would raise rights and privacy questions.
Regenerate with `node scripts/video/make_cctv.cjs` and `node scripts/video/record_demo.cjs [--leaflet <dir>]` (needs Node, Playwright and ffmpeg with libx264;
CI does not run them). The recordings were made offline, so their live map shows markers on a grid without map tiles.

## Configuration

| Env var | Purpose |
|---|---|
| `COPILOT_MODE` | `assist` (default) or `shadow` |
| `COPILOT_API_KEY` | If set, all `/api` routes need `X-API-Key` (WebSockets: `?token=`). Use SSO / OIDC in production |
| `COPILOT_AUDIT_KEY` | HMAC key for the audit chain (default is a dev key; use Vault / HSM) |
| `COPILOT_VAR` | Data directory (audit log, CAD mock, call archive) |
| `CAD_URL`, `CAD_TOKEN` | Use the generic `RestCAD` adapter instead of the mock |

## Replacing the baselines with real models

* **ASR:** implement `ASRBackend` (`speech.py`). `FasterWhisperBackend` is included for `/ws/audio/{call_id}` (16 kHz PCM16); it
  is untested and has no streaming partials. The target is a fine-tuned streaming model (Riva / NeMo / Conformer) that
  posts partials to `/segments`. `GET /api/vocabulary` returns the gazetteer words for contextual biasing.
* **Understanding:** `extract()` and `Reasoner` are the seams for a small neural extractor and an LLM running on stable segments.
* **CAD / telephony / GIS:** add an adapter per vendor behind `CADAdapter`; the gazetteer is `data/gazetteer.json`.

## What this MVP is not (read before quoting numbers)

* The sample SOP text, incident taxonomy, gazetteer and cue lexicons are **demo data**, not approved operational protocols.
  They must be replaced and signed off by the client's safety board before any live use.
* `python -m copilot.eval` scores 100% on the four bundled scenarios, but those scenarios were written together with the
  lexicons. This proves the pipeline works end to end, not that accuracy targets are met. WER, dialect accuracy, cue recall and
  location accuracy must be baselined on real call audio during the POC.
* Latency shown is in-process compute only (a few ms). The 800 ms budget also covers audio framing, ASR and network.
* Not built yet (P2/P3 in the catalogue): unit recommendation, CCTV/ANPR fusion, translation/TTS, major-incident mode,
  hoax detection, duplicate detection, QA scoring, training simulator, SIPREC/NG112 ingestion, multi-site HA.
* Single process, in-memory call state and event bus. Production needs Kafka/NATS, Redis state, Kubernetes and SSO.
