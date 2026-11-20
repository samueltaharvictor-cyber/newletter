"""Classification de titres d'actualité : résultats publiés / annonce de date / aperçu / autre.
Les nombres extraits (style « Q3 EPS $0.06 Misses $0.08 Estimate ») sont des CANDIDATS non vérifiés."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_Q_WORD = {"first": 1, "second": 2, "third": 3, "fourth": 4}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"], 1)}
_TOK = r"\(?-?\$?\(?-?\d[\d,]*(?:\.\d+)?\)?[KMBT]?"
_VERB = r"(?:Beats?|Miss(?:es)?|Meets?|Match(?:es)?|Tops?|Falls?\s+Short\s+Of|In[- ]Line(?:\s+With)?)"
_EPS = re.compile(rf"\bEPS\s+(?P<a>{_TOK})(?:\s+(?P<v>{_VERB}))?(?:\s+(?P<e>{_TOK}))?\s+Estimate", re.I)
_REV = re.compile(rf"\b(?:Sales|Revenue)\s+(?P<a>{_TOK})(?:\s+(?P<v>{_VERB}))?(?:\s+(?P<e>{_TOK}))?\s+Estimate", re.I)
_PREVIEW = re.compile(
    r"\b(preview|what to expect|ahead of (?:its |the )?(?:q[1-4]|earnings|results|quarter)|expected to (?:report|post)|"
    r"earnings (?:preview|alert|scheduled)|how to (?:play|trade)|set to report|will report|to report (?:earnings|after|before|today|tomorrow))\b",
    re.I)
_DATE = re.compile(
    r"\b(?:(?:to|will)\s+(?:announce|report|release|issue|host|present|discuss|provide|hold)|schedules?|scheduled|"
    r"announces?\s+(?:the\s+)?(?:dates?|timing)|conference call|webcast)\b", re.I)
_RESULT = re.compile(
    r"\b(?:reports?|reported|announces?|announced|posts?|delivers?|releases?|issues?)\b.*?"
    r"\b(?:(?:first|second|third|fourth)[- ]quarter|q[1-4]|[1-4]q|(?:full|fiscal)[- ]year|annual|year[- ]end)\b.*?"
    r"\b(?:results|earnings|performance)\b", re.I)
_NOISE = re.compile(r"\b(transcript|price targets?|analysts? (?:adjust|raise|lower|cut))\b", re.I)
_QUARTER = re.compile(r"\b(first|second|third|fourth)[- ]quarter\b|\bq([1-4])\b", re.I)
_LONG_DATE = re.compile(r"\b(" + "|".join(m.capitalize() for m in _MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b")


@dataclass(frozen=True)
class HeadlineInfo:
    kind: str                         # RESULT | DATE_ANNOUNCEMENT | PREVIEW | OTHER
    quarter: int | None = None
    eps_actual: float | None = None
    eps_estimate: float | None = None
    eps_verdict: str | None = None    # beat | miss | inline
    revenue_actual: float | None = None
    revenue_estimate: float | None = None
    revenue_verdict: str | None = None
    announced_date: date | None = None
    numbers_parsed: bool = False


def parse_money(tok: str | None) -> float | None:
    if not tok:
        return None
    m = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([KMBT])?", tok.strip(), re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", "")) * {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get((m.group(2) or "").upper(), 1)
    return -val if ("-" in tok or "(" in tok) else val


def _verdict(v: str | None) -> str | None:
    if not v:
        return None
    v = v.lower()
    if v.startswith(("beat", "top")):
        return "beat"
    if v.startswith(("miss", "fall")):
        return "miss"
    return "inline"


def _quarter(text: str) -> int | None:
    m = _QUARTER.search(text)
    if not m:
        return None
    return _Q_WORD[m.group(1).lower()] if m.group(1) else int(m.group(2))


def classify_headline(text: str) -> HeadlineInfo:
    h = " ".join((text or "").split())
    if not h or _NOISE.search(h):
        return HeadlineInfo("OTHER")
    q = _quarter(h)
    me = _EPS.search(h)
    if me:
        mr = _REV.search(h)
        return HeadlineInfo(
            "RESULT", quarter=q, eps_actual=parse_money(me["a"]), eps_estimate=parse_money(me["e"]),
            eps_verdict=_verdict(me["v"]),
            revenue_actual=parse_money(mr["a"]) if mr else None, revenue_estimate=parse_money(mr["e"]) if mr else None,
            revenue_verdict=_verdict(mr["v"]) if mr else None, numbers_parsed=True)
    if _PREVIEW.search(h):
        return HeadlineInfo("PREVIEW", quarter=q)
    m_res, m_date = _RESULT.search(h), _DATE.search(h)
    if m_date and (not m_res or m_date.start() <= m_res.start()):
        d = None
        md = _LONG_DATE.search(h)
        if md:
            try:
                d = date(int(md.group(3)), _MONTHS[md.group(1).lower()], int(md.group(2)))
            except ValueError:
                d = None
        return HeadlineInfo("DATE_ANNOUNCEMENT", quarter=q, announced_date=d)
    if m_res:
        return HeadlineInfo("RESULT", quarter=q)
    return HeadlineInfo("OTHER")
