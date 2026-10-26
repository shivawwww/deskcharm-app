"""Generate the sample call clips in web/audio/ (needs espeak-ng and ffmpeg; output is committed so the page build does not).

    python scripts/build_audio.py
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    out = ROOT / "web" / "audio"
    out.mkdir(exist_ok=True)
    for s in json.loads((ROOT / "copilot" / "data" / "audio_samples.json").read_text(encoding="utf-8"))["samples"]:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "x.wav"
            subprocess.run(["espeak-ng", "-v", s["voice"], "-s", "135", "-w", str(wav), s["text"]], check=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-ac", "1", "-ar", "22050", "-b:a", "48k", str(out / f"{s['id']}.mp3")], check=True)
        print("wrote", out / f"{s['id']}.mp3")


if __name__ == "__main__":
    main()
