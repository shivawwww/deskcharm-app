"""Fast extractor (features 9, 11, 12): incident type, entities, critical cues.

Deterministic, lexicon + regex based, English and Arabic (dialect-tolerant).
Runs on every partial transcript in well under a millisecond. It is the baseline
that a small neural extractor can replace behind the same `extract()` signature;
cue patterns are deliberately broad because a missed cue is the worst error.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .text import compile_terms, negated, normalize, norm, original_span

# ---------------------------------------------------------------- critical cues
# id -> (label, level, negatable, terms)
CUES: dict[str, tuple[str, str, bool, list[str]]] = {
    "not_breathing": ("Not breathing / no pulse", "critical", False, [
        r"not breathing", r"isn't breathing", r"stopped breathing", r"no breath", r"no pulse",
        r"cardiac arrest", r"heart stopped", r"stopped beating",
        "لا يتنفس", "ما يتنفس", "مو يتنفس", "مب يتنفس", "مش يتنفس", "ما عم يتنفس", "توقف قلبه", "قلبه وقف", "ما فيه نبض", "لا نبض", "توقف التنفس"]),
    "unconscious": ("Unconscious / unresponsive", "critical", False, [
        r"unconscious", r"unresponsive", r"passed out", r"collapsed", r"not responding", r"won't wake", r"fainted",
        "فاقد الوعي", "فاقدة الوعي", "مغمى عليه", "اغمي عليه", "ما يستجيب", "مش واعي", "مو واعي", "ما عم يرد", "مش بيرد", "ما يرد"]),
    "breathing_difficulty": ("Difficulty breathing / choking", "critical", False, [
        r"can't breathe", r"cannot breathe", r"struggling to breathe", r"short of breath", r"difficulty breathing", r"choking",
        "ما يقدر يتنفس", "صعوبه في التنفس", "ضيق نفس", "ضيق تنفس", "يختنق", "مختنق", "مش قادر يتنفس"]),
    "heavy_bleeding": ("Bleeding reported", "high", False, [
        r"bleed\w*", r"lots? of blood", r"blood everywhere", r"hemorrhag\w*",
        "ينزف", "نزيف", "دم كثير", "دم كتير", "دماء", "دم في كل مكان"]),
    "weapon": ("Weapon involved", "critical", True, [
        r"guns?", r"knife", r"knives", r"firearm", r"pistol", r"rifle", r"weapons?", r"machete", r"armed", r"sword",
        "سلاح", "مسدس", "سكين", "رشاش", "بندقيه", "سيف", "مسلح"]),
    "stabbing_shooting": ("Stabbing / shooting", "critical", True, [
        r"stabb\w*", r"stabbed", r"shot (?:him|her|me|dead)", r"gunshots?", r"gunfire", r"shots fired", r"shooting",
        "طعن", "طعنه", "اطلاق نار", "ضرب نار", "رصاص", "رصاصه", "مضروب بالنار"]),
    "child": ("Child / infant involved", "high", False, [
        r"child", r"children", r"kids?", r"baby", r"infant", r"toddler", r"newborn",
        "طفل", "اطفال", "طفله", "رضيع", "بيبي", "عيال", "عيل", "اولاد صغار", "ولد صغير", "بنت صغيره"]),
    "fire_spreading": ("Fire spreading / explosion", "critical", True, [
        r"spreading", r"spread", r"getting bigger", r"out of control", r"engulf\w*", r"whole building", r"explo\w+",
        "ينتشر", "تنتشر", "انتشر", "انتشرت", "امتدت", "تمتد", "خارج السيطره", "انفجار", "كل المبني", "بتكبر", "عم تكبر"]),
    "trapped": ("Person(s) trapped", "critical", True, [
        r"trapped", r"stuck (?:inside|in)", r"can't get out", r"locked in", r"pinned", r"under (?:the )?(?:rubble|car|debris)",
        "محاصر", "عالق", "محبوس", "محتجز", "ما نقدر نطلع", "ما يقدر يطلع", "تحت الانقاض"]),
    "hazard_gas": ("Gas / chemical hazard", "high", True, [
        r"gas leak", r"smell of gas", r"smells? (?:like )?gas", r"chemicals?", r"explosives?", r"fuel leak",
        "تسرب غاز", "ريحه غاز", "مواد كيميائيه", "تسرب وقود"]),
}
_CUE_RE = {cid: compile_terms(terms) for cid, (_, _, _, terms) in CUES.items()}

# ------------------------------------------------------------- incident types
# type -> list of (term, weight)
TYPES: dict[str, list[tuple[str, float]]] = {
    "FIRE": [(r"fires?", 1.5), (r"flames?", 1.5), (r"smoke", 1.0), (r"burning", 1.5), (r"blaze", 1.5),
             ("حريق", 2.0), ("النار", 1.5), ("نار", 1.5), ("دخان", 1.0), ("يحترق", 1.5), ("اشتعال", 1.5), ("ولعت", 1.5)],
    "MEDICAL": [(r"heart attack", 2.0), (r"chest pain", 2.0), (r"cardiac", 2.0), (r"seizure", 2.0), (r"stroke", 2.0),
                (r"ambulance", 1.0), (r"injur\w+", 1.0), (r"overdose", 2.0), (r"pregnan\w*", 1.5), (r"labou?r", 1.0),
                (r"sick", 1.0), (r"hurt", 0.8), (r"diabet\w*", 1.0), (r"patient", 1.0),
                ("نوبه قلبيه", 2.0), ("جلطه", 2.0), ("الم في الصدر", 2.0), ("اسعاف", 1.0), ("مصاب", 0.8), ("مريض", 1.0),
                ("تشنج", 2.0), ("حامل", 1.5), ("ولاده", 1.5)],
    "TRAFFIC": [(r"accident", 1.5), (r"crash\w*", 1.5), (r"collision", 2.0), (r"collided", 2.0), (r"hit by (?:a )?car", 2.0),
                (r"rolled over", 2.0), (r"overturned", 2.0), (r"run over", 2.0), (r"vehicles?", 0.5), (r"cars?", 0.5),
                (r"motorcycle", 0.5), (r"truck", 0.5),
                ("حادث", 1.5), ("تصادم", 2.0), ("اصطدام", 2.0), ("دهس", 2.0), ("انقلاب", 2.0), ("انقلبت", 2.0),
                ("سياره", 0.5), ("سيارتين", 0.5), ("سيارات", 0.5), ("شاحنه", 0.5), ("دراجه", 0.5), ("صدم", 1.5)],
    "CRIME": [(r"fight\w*", 1.5), (r"assault\w*", 2.0), (r"attack\w*", 2.0), (r"robb\w+", 2.0), (r"stolen", 1.5),
              (r"burglar\w*", 2.0), (r"break-?in", 2.0), (r"intruder", 2.0), (r"threaten\w*", 1.5), (r"kidnap\w*", 2.0),
              (r"domestic", 1.0), (r"rape\w*", 2.0),
              ("سرقه", 2.0), ("سارق", 2.0), ("حرامي", 2.0), ("مشاجره", 2.0), ("عراك", 2.0), ("اعتداء", 2.0), ("هجوم", 2.0),
              ("يهاجم", 2.0), ("تهديد", 1.5), ("خطف", 2.0), ("اقتحام", 2.0), ("تحرش", 2.0), ("اغتصاب", 2.0), ("يضربني", 2.0)],
}
_TYPE_RE = {t: [(compile_terms([term]), w) for term, w in items] for t, items in TYPES.items()}
# cues push the type decision (a weapon is a crime scene, no breathing is medical ...)
_CUE_TYPE_BOOST = {
    "not_breathing": ("MEDICAL", 2.0), "unconscious": ("MEDICAL", 1.5), "breathing_difficulty": ("MEDICAL", 2.0),
    "heavy_bleeding": ("MEDICAL", 0.5), "weapon": ("CRIME", 2.0), "stabbing_shooting": ("CRIME", 2.0),
    "fire_spreading": ("FIRE", 1.0),
}

# --------------------------------------------------------------------- entities
_NUM_EN = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
           "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
           "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
           "seventy": 70, "eighty": 80, "ninety": 90}
_NUM_AR = {norm(k): v for k, v in {"واحد": 1, "اثنين": 2, "اتنين": 2, "اثنان": 2, "ثلاثه": 3, "اربعه": 4, "خمسه": 5, "سته": 6,
                                    "سبعه": 7, "ثمانيه": 8, "تسعه": 9, "عشره": 10}.items()}
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_UNITS = "one|two|three|four|five|six|seven|eight|nine"
_NUMWORD_EN = rf"(?:(?:{_TENS})(?:[ -](?:{_UNITS}))?|{'|'.join(k for k in _NUM_EN if k not in _TENS.split('|'))})"
_NUMWORD_AR = "|".join(sorted(_NUM_AR, key=len, reverse=True))
_NUM = rf"(\d{{1,3}}|{_NUMWORD_EN}|{_NUMWORD_AR})"

_AGE_RES = [
    re.compile(rf"{_NUM}\s*(?:years?|yrs?)\s*old"),
    re.compile(rf"\bage[d]?\s*{_NUM}"),
    re.compile(rf"(?:عمر\w*|بعمر)\s*{_NUM}"),
    re.compile(rf"{_NUM}\s*(?:سنه|سنين|سنوات|عام)"),
]
_MONTHS_RE = re.compile(rf"{_NUM}\s*months?\s*old")
_PERSON_RES = [
    re.compile(rf"\b{_NUM}\s+(?:people|persons|men|women|workers|employees|passengers|residents|victims|injured|casualties|of us)"),
    re.compile(rf"(?<!\w){_NUM}\s*(?:اشخاص|شخص|مصابين|مصابي|جرحي|ناس|رجال|نساء)"),
]
_PLATE_RE = re.compile(r"(?:number plate|license plate|plate|registration|لوحه|لوحة)\s*(?:number|is|رقم|:)?\s*([A-Za-zء-ي]{0,2}\s?\d{1,5})")
_DETAIL_RES = [
    (re.compile(r"\b(building|bldg|villa|flat|apartment|apt|tower|unit|room)\s*(?:number|no|#)?\s*(\d+[a-z]?)"), None),
    (re.compile(r"(?:ال)?(?:مبني|عماره|بيت|فيلا|شقه|برج|نمره|بناية)\s*(?:رقم|نمره)?\s*(\d+)"), "building"),
    (re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+floor"), "floor_en"),
    (re.compile(r"\bfloor\s*(\d+)"), "floor"),
    (re.compile(r"(?:ال)?(?:دور|طابق)\s+(?:ال)?(اول|ثاني|ثالث|رابع|خامس|سادس|سابع|ثامن|تاسع|عاشر|\d+)"), "floor_ar"),
]
_ORD_EN = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_ORD_AR = {"اول": 1, "ثاني": 2, "ثالث": 3, "رابع": 4, "خامس": 5, "سادس": 6, "سابع": 7, "ثامن": 8, "تاسع": 9, "عاشر": 10}

_INJURY_TERMS = {
    "bleeding": [r"bleed\w*", r"blood", "ينزف", "نزيف", "دم", "دماء"],
    "fracture": [r"broken (?:arm|leg|bone|back|neck)", r"fractur\w*", "كسر"],
    "burns": [r"burn(?:s|ed|t)", "حروق", "محروق"],
    "chest pain": [r"chest pain", "الم في الصدر", "ألم في الصدر"],
    "head injury": [r"head (?:injury|wound)", r"hit (?:his|her) head", "اصابه في الراس"],
    "wound": [r"wound\w*", r"stabbed", "جرح", "طعن"],
    "unconscious": [r"unconscious", r"collapsed", "فاقد الوعي", "مغمى عليه"],
    "injured": [r"injur\w+", r"hurt", "مصاب", "اتصاب", "جريح", "جرحي"],
}
_INJURY_RE = {k: compile_terms(v) for k, v in _INJURY_TERMS.items()}
_VEHICLE_TERMS = {
    "car": [r"cars?", "سياره", "سيارتين", "سيارات"], "truck": [r"truck", r"lorry", "شاحنه"],
    "bus": [r"bus", "باص", "حافله"], "motorcycle": [r"motorcycle", r"motorbike", r"bike", "دراجه", "دباب"],
    "van": [r"van", "فان"], "taxi": [r"taxi", "تاكسي"],
}
_VEHICLE_RE = {k: compile_terms(v) for k, v in _VEHICLE_TERMS.items()}
_HAZARD_TERMS = {
    "smoke": [r"smoke", "دخان"], "gas": [r"gas leak", r"smell of gas", "تسرب غاز", "ريحه غاز"],
    "fuel": [r"fuel", r"petrol", r"gasoline", "وقود", "بنزين"], "chemicals": [r"chemicals?", "مواد كيميائيه"],
    "explosion": [r"explo\w+", "انفجار"], "electrical": [r"electri\w+", r"power line", r"sparks?", "كهرباء", "شرر"],
}
_HAZARD_RE = {k: compile_terms(v) for k, v in _HAZARD_TERMS.items()}

# ----------------------------------------------- "has the caller covered this?" slots
_ANSWER_RE = {
    "conscious_breathing": compile_terms([r"breath\w*", r"conscious", r"awake", r"responding", r"responsive",
                                          "يتنفس", "واعي", "وعي", "يرد", "يستجيب", "نفس"]),
    "fire_status": compile_terms([r"spread\w*", r"contained", r"small", r"big", r"out of control", r"growing",
                                  "ينتشر", "صغير", "كبير", "السيطره", "تنتشر"]),
    "people_inside": compile_terms([r"inside", r"trapped", r"everyone (?:is )?out", r"evacuated", r"nobody", r"no one",
                                    "داخل", "فوق", "عالق", "اخلاء", "ما فيه احد", "محد", "برا"]),
    "weapon": compile_terms([r"unarmed", r"no weapon", "بدون سلاح", "ما معه سلاح"]),
    "suspect_present": compile_terms([r"still (?:there|here|inside)", r"ran away", r"fled", r"escaped", r"left",
                                      "هرب", "فر", "لسه موجود", "لسا موجود", "ما زال", "راح"]),
}


@dataclass
class CueHit:
    id: str
    label: str
    level: str
    evidence: str


@dataclass
class Extraction:
    incident_type: str = "UNKNOWN"
    incident_conf: float = 0.0
    type_scores: dict[str, float] = field(default_factory=dict)
    type_evidence: list[str] = field(default_factory=list)
    cues: list[CueHit] = field(default_factory=list)
    persons: int | None = None
    persons_evidence: str = ""
    ages: list[int] = field(default_factory=list)
    ages_evidence: list[str] = field(default_factory=list)
    injuries: list[str] = field(default_factory=list)
    weapons: list[str] = field(default_factory=list)
    vehicles: list[str] = field(default_factory=list)
    plates: list[str] = field(default_factory=list)
    hazards: list[str] = field(default_factory=list)
    address_detail: list[str] = field(default_factory=list)
    answered: set[str] = field(default_factory=set)


def _to_int(tok: str) -> int | None:
    tok = tok.strip().replace("-", " ")
    if tok.isdigit():
        return int(tok)
    if tok in _NUM_AR:
        return _NUM_AR[tok]
    parts = tok.split()
    if parts and all(p in _NUM_EN for p in parts):
        return sum(_NUM_EN[p] for p in parts)
    return None


def extract(text: str) -> Extraction:
    ex = Extraction()
    n, idx = normalize(text)
    ev = lambda s, e: original_span(text, idx, s, e)  # noqa: E731

    # cues -------------------------------------------------------------------
    for cid, (label, level, neg_ok, _) in CUES.items():
        for m in _CUE_RE[cid].finditer(n):
            if neg_ok and negated(n, m.start()):
                continue
            ex.cues.append(CueHit(cid, label, level, ev(m.start(), m.end())))
            break
    cue_ids = {c.id for c in ex.cues}

    # entities -----------------------------------------------------------------
    ages: list[tuple[int, str]] = []
    for r in _AGE_RES:
        for m in r.finditer(n):
            v = _to_int(m.group(1))
            if v is not None and 0 <= v <= 110:
                ages.append((v, ev(m.start(), m.end())))
    for m in _MONTHS_RE.finditer(n):
        ages.append((0, ev(m.start(), m.end())))
    seen: set[int] = set()
    for v, e in ages:
        if v not in seen:
            seen.add(v)
            ex.ages.append(v)
            ex.ages_evidence.append(e)
    if any(a <= 12 for a in ex.ages) and "child" not in cue_ids:
        i = next(i for i, a in enumerate(ex.ages) if a <= 12)
        ex.cues.append(CueHit("child", CUES["child"][0], CUES["child"][1], ex.ages_evidence[i]))

    best = 0
    for r in _PERSON_RES:
        for m in r.finditer(n):
            v = _to_int(m.group(1))
            if v and v > best and v < 200:
                best, ex.persons_evidence = v, ev(m.start(), m.end())
    ex.persons = best or None

    ex.injuries = [k for k, r in _INJURY_RE.items() if r.search(n)]
    ex.vehicles = [k for k, r in _VEHICLE_RE.items() if r.search(n)]
    ex.hazards = [k for k, r in _HAZARD_RE.items() if r.search(n)]
    ex.weapons = [c.evidence for c in ex.cues if c.id in ("weapon",)]
    ex.plates = [re.sub(r"\s+", "", m.group(1)).upper() for m in _PLATE_RE.finditer(text)]

    for r, kind in _DETAIL_RES:
        for m in r.finditer(n):
            if kind is None:
                ex.address_detail.append(f"{m.group(1)} {m.group(2)}")
            elif kind == "building":
                ex.address_detail.append(f"building {m.group(1)}")
            elif kind == "floor_en":
                ex.address_detail.append(f"floor {_ORD_EN[m.group(1)]}")
            elif kind == "floor":
                ex.address_detail.append(f"floor {m.group(1)}")
            else:
                g = m.group(1)
                ex.address_detail.append(f"floor {_ORD_AR.get(g, g)}")
    ex.address_detail = list(dict.fromkeys(ex.address_detail))

    # incident type ------------------------------------------------------------
    scores = {t: 0.0 for t in TYPES}
    evidence: dict[str, list[str]] = {t: [] for t in TYPES}
    for t, items in _TYPE_RE.items():
        for rx, w in items:
            m = rx.search(n)
            if m:
                scores[t] += w
                evidence[t].append(ev(m.start(), m.end()))
    for cid in cue_ids:
        if cid in _CUE_TYPE_BOOST:
            t, w = _CUE_TYPE_BOOST[cid]
            scores[t] += w
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top_t, top = ranked[0]
    total = sum(scores.values())
    ex.type_scores = {t: round(s, 2) for t, s in scores.items()}
    if top >= 1.5:
        ex.incident_type = top_t
        ex.incident_conf = round(min(0.99, (top / total) * (1 - math.exp(-top / 1.5))), 2)
        ex.type_evidence = evidence[top_t][:3]
    elif top >= 0.9:
        ex.incident_type = top_t
        ex.incident_conf = round(min(0.45, (top / total) * (1 - math.exp(-top / 1.5))), 2)
        ex.type_evidence = evidence[top_t][:3]

    # answered slots -------------------------------------------------------------
    for slot, rx in _ANSWER_RE.items():
        if rx.search(n):
            ex.answered.add(slot)
    if cue_ids & {"not_breathing", "unconscious", "breathing_difficulty"}:
        ex.answered.add("conscious_breathing")
    if cue_ids & {"weapon", "stabbing_shooting"}:
        ex.answered.add("weapon")
    if cue_ids & {"fire_spreading"}:
        ex.answered.add("fire_status")
    if cue_ids & {"trapped"}:
        ex.answered.add("people_inside")
    return ex
