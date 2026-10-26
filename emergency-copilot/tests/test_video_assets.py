"""The demo videos are committed binaries: make sure they exist, are real MP4s, stay small, and the pages point at them."""
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
CLIPS = ["demo_ccc_simulation", "demo_usecases_tour", "cctv_collision", "cctv_fire", "cctv_crime", "cctv_medical"]


def test_clips_and_posters_exist_and_are_valid_mp4():
    total = 0
    for name in CLIPS:
        mp4, jpg = WEB / "video" / f"{name}.mp4", WEB / "video" / f"{name}.jpg"
        assert mp4.exists() and jpg.exists(), name
        head = mp4.read_bytes()[:12]
        assert head[4:8] == b"ftyp", f"{name} is not an MP4"
        assert jpg.read_bytes()[:2] == b"\xff\xd8", f"{name} poster is not a JPEG"
        total += mp4.stat().st_size
        assert mp4.stat().st_size < 4_000_000, f"{name} is too large for a repo asset"
    assert total < 8_000_000


def test_pages_reference_only_existing_videos():
    for page in ("usecases.html", "ccc.html"):
        html = (WEB / page).read_text(encoding="utf-8")
        refs = set(re.findall(r"video/([a-z_]+\.(?:mp4|jpg))", html))
        refs |= {f"cctv_{c}.mp4" for c in re.findall(r'collision|fire|crime|medical', html)} if page == "ccc.html" else set()
        for r in refs:
            assert (WEB / "video" / r).exists(), (page, r)
    html = (WEB / "usecases.html").read_text(encoding="utf-8")
    assert html.count("<video") == 6 and "NOT REAL FOOTAGE" not in html  # clips are labelled inside the video frames, captions say simulated
    assert "not real footage" in html.lower()
