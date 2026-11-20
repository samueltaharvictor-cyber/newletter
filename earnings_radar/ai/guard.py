"""Garde-fou « zéro chiffre inventé » pour toute synthèse produite par une IA locale :
chaque nombre du texte doit exister dans les faits fournis (au plus à l'arrondi près) — sinon la synthèse est rejetée."""
from __future__ import annotations

import re
from dataclasses import dataclass

_NUM = re.compile(r"(?<![\w.])-?\(?\$?\(?-?\d[\d,]*(?:\.\d+)?\)?\s?(?:%|[KMBT]\b)?")
_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def _to_float(tok: str) -> float | None:
    t = tok.strip()
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s?(%|[KMBT])?", t)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    if m.group(2) in _MULT:
        v *= _MULT[m.group(2)]
    return -v if ("-" in t or "(" in t) else v


def extract_numbers(text: str) -> list[float]:
    text = re.sub(r"(?<=\d),(?=\d{1,2}(?!\d))", ".", text)   # virgule décimale française : 328,8 -> 328.8
    return [v for v in (_to_float(m.group(0)) for m in _NUM.finditer(text)) if v is not None]


def _flatten(facts) -> list[float]:
    out: list[float] = []
    if isinstance(facts, dict):
        for v in facts.values():
            out += _flatten(v)
    elif isinstance(facts, (list, tuple)):
        for v in facts:
            out += _flatten(v)
    elif isinstance(facts, bool):
        pass
    elif isinstance(facts, (int, float)):
        out.append(float(facts))
    elif isinstance(facts, str):
        out += extract_numbers(facts)
    return out


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    unknown: tuple[float, ...]


def verify_summary(summary: str, facts, *, rel_tol: float = 0.005) -> GuardResult:
    known = _flatten(facts)
    unknown: list[float] = []
    for n in extract_numbers(summary):
        if 1990 <= n <= 2100 and float(n).is_integer():
            continue          # années (les « Q3 » ne sont pas extraits)
        if not any(abs(n - k) <= max(abs(k) * rel_tol, 0.0051) or abs(abs(n) - abs(k)) <= max(abs(k) * rel_tol, 0.0051) for k in known):
            unknown.append(n)
    return GuardResult(not unknown, tuple(unknown))
