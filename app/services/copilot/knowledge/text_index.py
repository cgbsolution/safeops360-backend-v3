"""A small BM25 index for the copilot's reference libraries.

The libraries are a few thousand short records, so an in-memory index built
once per process is enough: no database table, no extension, no network call.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

_STOP = frozenset(
    "a an and are as at be been being by for from has have in into is it its of on or that the this to was "
    "were will with near due which there their they them then than also very more any some all not no "
    "should shall can may might must being done found observed provided kept lying area line section".split()
)

# Plant vocabulary folded onto one term, so "handrail", "hand rail" and
# "railing" find each other. Keys and values are post-normalisation tokens.
_SYNONYMS: dict[str, str] = {
    "handrail": "railing", "guardrail": "railing", "rail": "railing", "railings": "railing",
    "barricade": "barrier", "barricading": "barrier", "cordon": "barrier",
    "wire": "cable", "wiring": "cable", "cables": "cable", "conductor": "cable",
    "db": "panel", "mcc": "panel", "jb": "panel", "junction": "panel", "switchboard": "panel",
    "earth": "earthing", "grounding": "earthing", "ground": "earthing",
    "spillage": "spill", "leakage": "leak", "leaking": "leak", "seepage": "leak",
    "slippery": "slip", "slipping": "slip", "tripping": "trip",
    "light": "lighting", "illumination": "lighting", "lights": "lighting", "lamp": "lighting",
    "staircase": "stair", "stairs": "stair", "steps": "stair", "step": "stair",
    "grating": "grating", "gratings": "grating", "chequered": "plate",
    "hoist": "crane", "eot": "crane", "sling": "tackle", "shackle": "tackle", "rope": "tackle",
    "extinguisher": "extinguisher", "dcp": "extinguisher", "hydrant": "hydrant",
    "forklift": "vehicle", "truck": "vehicle", "trailer": "vehicle", "dumper": "vehicle", "hydra": "vehicle",
    "payloader": "vehicle", "bobcat": "vehicle", "loader": "vehicle", "tractor": "vehicle",
    "coupling": "rotating", "shaft": "rotating", "pulley": "rotating", "gear": "rotating", "spindle": "rotating",
    "unguarded": "guard", "guarding": "guard", "cover": "guard", "covers": "guard",
    "helmet": "ppe", "goggles": "ppe", "gloves": "ppe", "harness": "harness", "shoes": "ppe",
    "molten": "molten", "liquid": "molten", "slag": "slag", "ladle": "ladle",
    "obstruction": "obstruct", "obstructed": "obstruct", "blocked": "obstruct", "blocking": "obstruct",
    "scrap": "scrap", "debris": "scrap", "waste": "scrap", "garbage": "scrap",
    "pit": "opening", "hole": "opening", "manhole": "opening", "trench": "opening",
}


def _norm(tok: str) -> str:
    t = tok
    for suf in ("ings", "ing", "ies", "ed", "es", "s"):
        if len(t) > 4 and t.endswith(suf):
            t = t[: -len(suf)] + ("y" if suf == "ies" else "")
            break
    return t


def tokenize(text: str | None) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        if raw in _STOP or len(raw) < 2 or raw.isdigit():
            continue
        tok = _SYNONYMS.get(raw) or _SYNONYMS.get(_norm(raw)) or _norm(raw)
        out.append(tok)
    return out


class Bm25Index:
    """Okapi BM25 over pre-tokenised documents."""

    def __init__(self, docs: Iterable[list[str]], k1: float = 1.4, b: float = 0.7) -> None:
        self.docs = [Counter(d) for d in docs]
        self.lengths = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lengths) / len(self.lengths)) if self.lengths else 1.0
        self.k1, self.b = k1, b
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.postings: dict[str, list[int]] = {}
        for i, d in enumerate(self.docs):
            for t in d:
                self.postings.setdefault(t, []).append(i)

    def search(self, query: list[str], limit: int = 10, allow: set[int] | None = None) -> list[tuple[int, float]]:
        scores: dict[int, float] = {}
        for t in set(query):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.postings[t]:
                if allow is not None and i not in allow:
                    continue
                tf = self.docs[i][t]
                denom = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / self.avg)
                scores[i] = scores.get(i, 0.0) + idf * tf * (self.k1 + 1) / denom
        return sorted(scores.items(), key=lambda kv: -kv[1])[:limit]
