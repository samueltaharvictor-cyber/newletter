"""Repère, dans un communiqué de résultats, une HAUSSE du chiffre d'affaires du trimestre (et elle seule).
Heuristiques volontairement prudentes : au moindre doute (baisse, trimestre précédent, prévision, une seule branche d'activité,
année entière), on ne dit rien. Les résultats à la baisse ou stables ne produisent jamais de message."""
from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

_BLOCKS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCKS:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCKS:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    text = "".join(p.out).replace("\xa0", " ")
    return "\n".join(" ".join(line.split()) for line in text.split("\n") if line.strip())


_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z$“\"(•])|\n+")
_CLAUSE = re.compile(r";| but | while | whereas |, and (?=net |operating |gross |adjusted |earnings |diluted |cash )", re.I)
_GUIDE = re.compile(r"\b(guidance|outlook|expects?|expected|forecast\w*|anticipat\w*|project\w*|target\w*|estimat\w*|consensus|plans? to|will|should|may|could|raised?|raises|reaffirm\w*)\b", re.I)
_LONG = re.compile(r"(full[- ]year|fiscal year|year[- ]to[- ]date|nine months|six months|twelve months|annual)", re.I)
_QTR = re.compile(r"(quarter|\bq[1-4]\b|three months)", re.I)
_QOQ = re.compile(r"(sequential\w*|quarter[- ]over[- ]quarter|(?:from|than|vs\.?|versus|to) (?:the )?(?:previous|preceding|prior|last) quarter\b(?! of))", re.I)
_REV = re.compile(r"\b(?:(?P<mod>[A-Za-z0-9&.\-]+)\s+)?(?P<kw>revenues?|net sales|total sales)\b", re.I)
_BAD_MOD = {"cloud", "service", "services", "subscription", "product", "products", "software", "hardware", "license", "licensing", "advertising",
            "segment", "international", "domestic", "recurring", "royalty", "rental", "interest", "other", "data", "retail", "wholesale",
            "commercial", "consumer", "enterprise", "government", "automotive", "regional", "same-store", "comparable", "organic", "deferred",
            "cost", "costs", "marketing", "maintenance", "collaboration", "grant", "license", "partner", "platform", "online", "us", "u.s."}
_UP = re.compile(r"\b(increas\w*|grew|grow\w*|rose|rise\w*|up|jump\w*|climb\w*|surg\w*|soar\w*|expand\w*|improv\w*|gain\w*|higher|advanc\w*|growth|increase)\b", re.I)
_DOWN = re.compile(r"\b(decreas\w*|declin\w*|fell|fall\w*|drop\w*|down|lower|reduc\w*|shr[au]nk|contract\w*|slid|slip\w*|dip\w*|loss)\b", re.I)
_PCT = re.compile(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?:%|percent\b)", re.I)
_AMT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s?(billion|million|thousand|bn|mm|m|b|k)?\b", re.I)
_CMP = re.compile(r"(compared (?:to|with)|versus|vs\.?|\bfrom\b|year[- ]ago|prior[- ]year|same (?:period|quarter))", re.I)
_WORD = re.compile(r"(?P<more>more than |over |nearly |almost )?\b(?P<w>doubled|tripled|quadrupled)\b", re.I)
_BEAT = re.compile(r"\b(exceed\w*|beat\w*|surpass\w*|top(?:ped)?)\s+(?:the\s+)?(?:analyst|consensus|street|market|wall street)s?['’]?\s+(?:revenue |sales )?(?:expectations|estimates|consensus)", re.I)
_UNIT = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


@dataclass(frozen=True)
class Finding:
    growth_pct: float
    kind: str          # explicite (pourcentage écrit) | calculé (deux montants) | mot (« doublé »…)
    sentence: str


@dataclass(frozen=True)
class Result:
    growth_pct: float | None
    kind: str
    label: str         # phrase française prête à afficher
    sentence: str
    beats: bool = False


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _amt(m: re.Match) -> float:
    return _num(m.group(1)) * _UNIT.get((m.group(2) or "").lower(), 1.0)


def _clause_finding(clause: str, sentence: str) -> Finding | None:
    rev = next((m for m in _REV.finditer(clause) if (m.group("mod") or "").lower() not in _BAD_MOD), None)
    if rev is None or _QOQ.search(clause):
        return None
    tail = clause[rev.start():]
    w = _WORD.search(tail)
    if w and w.start() < 60:
        g = {"doubled": 100.0, "tripled": 200.0, "quadrupled": 300.0}[w.group("w").lower()]
        return Finding(g, "mot", sentence)
    p = _PCT.search(tail)
    if p:
        before = tail[:p.start()]
        verbs = [(m.start(), "up" if _UP.fullmatch(m.group(0)) else "down") for m in re.finditer(_UP.pattern + "|" + _DOWN.pattern, before, re.I)]
        if not verbs or verbs[-1][1] == "down":
            return None
        return Finding(_num(p.group(1)), "explicite", sentence)
    amts = list(_AMT.finditer(tail))
    if len(amts) >= 2 and _CMP.search(tail[amts[0].end():amts[1].start() + 1]) and not _DOWN.search(tail[:amts[0].start()]):
        cur, prev = _amt(amts[0]), _amt(amts[1])
        if prev > 0 and cur > prev:
            return Finding((cur / prev - 1) * 100, "calculé", sentence)
    return None


def find_growth(text: str) -> list[Finding]:
    out: list[Finding] = []
    for sent in _SENT.split(text):
        if _GUIDE.search(sent) or (_LONG.search(sent) and not _QTR.search(sent)):
            continue
        for clause in _CLAUSE.split(sent):
            f = _clause_finding(clause, sent.strip())
            if f and f.growth_pct < 100000:
                out.append(f)
    return out


def describe(g: float, kind: str = "explicite", more_than: bool = False) -> str:
    r = 1 + g / 100
    if r >= 4:
        base = "a quadruplé"
    elif r >= 3:
        base = "a triplé"
    elif r >= 2:
        base = "a doublé"
    elif r >= 1.5:
        base = "est en très forte hausse"
    else:
        base = "est en hausse"
    shown = f" (+{g:.0f} %)" if g >= 10 else f" (+{g:.1f} %)".replace(".", ",")
    return f"Le chiffre d’affaires {base}{shown if kind != 'mot' else ''}"


def analyze_release(text: str, min_growth: float = 25.0) -> Result | None:
    """None = rien à signaler (baisse, stable, trop faible, incertain)."""
    beats = next((s.strip() for s in _SENT.split(text) if _BEAT.search(s) and re.search(r"revenue|sales", s, re.I)), None)
    findings = find_growth(text)
    first = findings[0] if findings else None      # le communiqué ouvre par le chiffre d'affaires total
    if first and first.growth_pct >= min_growth:
        label = describe(first.growth_pct, first.kind)
        if beats:
            label += ", au-dessus des attentes des analystes"
        return Result(first.growth_pct, first.kind, label, first.sentence, bool(beats))
    if beats:
        return Result(first.growth_pct if first else None, "attentes", "Le chiffre d’affaires est au-dessus des attentes des analystes", beats, True)
    return None
