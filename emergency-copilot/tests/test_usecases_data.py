"""The five-language use-case data and the page art must stay consistent."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((ROOT / "copilot" / "data" / "usecases.json").read_text(encoding="utf-8"))
LANGS = {"en", "ar", "hi", "ta", "ml"}


def art_scenes():
    src = (ROOT / "web" / "art.js").read_text(encoding="utf-8")
    return set(re.findall(r"^    (\w+): \(u\) =>", src, re.M))


def test_every_use_case_is_complete_in_all_languages():
    assert set(DATA["ui"]) == LANGS and len(DATA["usecases"]) == 25
    for u in DATA["usecases"]:
        assert set(u["tr"]) == LANGS, u["id"]
        n = len(u["tr"]["en"]["does"])
        for lang, t in u["tr"].items():
            assert t["title"].strip() and t["sit"].strip() and t["human"].strip(), (u["id"], lang)
            assert len(t["does"]) == n and all(d.strip() for d in t["does"]), (u["id"], lang)


def test_ui_strings_cover_every_language_and_category():
    keys = set(DATA["ui"]["en"])
    for lang, ui in DATA["ui"].items():
        assert set(ui) == keys, lang
        assert len(ui["cats"]) == len(DATA["cats"]) and set(ui["roles"]) == set(DATA["ui"]["en"]["roles"])
    assert {u["cat"] for u in DATA["usecases"]} <= set(DATA["cats"])
    assert {u["role"] for u in DATA["usecases"]} <= set(DATA["ui"]["en"]["roles"])


def test_every_use_case_has_its_own_illustration():
    scenes = art_scenes()
    used = [u["scene"] for u in DATA["usecases"]]
    assert set(used) <= scenes and len(set(used)) == len(used) == 25


def test_non_english_text_is_really_translated():
    for u in DATA["usecases"]:
        en = u["tr"]["en"]["title"]
        for lang in LANGS - {"en"}:
            assert u["tr"][lang]["title"] != en, (u["id"], lang)


def test_scenarios_referenced_by_use_cases_exist():
    from copilot.speech import load_scenarios
    sc = load_scenarios()
    assert {u["sc"] for u in DATA["usecases"] if u["sc"]} <= set(sc)


def test_built_pages_embed_the_data():
    html = (ROOT / "web" / "usecases.html").read_text(encoding="utf-8")
    assert html.count('"scene":') == 25 and "const ART" in html and "வீட்டில் இதயம்" in html and "Thirumal Dharmalingam" in html
