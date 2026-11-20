"""Analyse de variance des résultats (méthodes des skills finance : variance-analysis / financial-statements).
Pur et déterministe : aucun appel IA, aucun I/O. Fondation de l'Étape 2 (résultats vs consensus et vs historique)."""
from __future__ import annotations

from dataclasses import dataclass


def surprise_pct(actual: float | None, consensus: float | None) -> float | None:
    """(Réel − Consensus) / |Consensus| × 100. None si consensus ≈ 0 (formule trompeuse, cf. Master Prompt §6)."""
    if actual is None or consensus is None or abs(consensus) < 1e-9:
        return None
    return (actual - consensus) / abs(consensus) * 100.0


def checked_surprise(actual: float | None, actual_basis: str, consensus: float | None, consensus_basis: str) -> dict:
    """Refuse de comparer BPA GAAP et BPA ajusté (V2 §5) : renvoie la surprise ou l'avertissement."""
    if actual_basis != consensus_basis:
        return {"value": None, "warning": f"bases différentes ({actual_basis} vs {consensus_basis}) : comparaison refusée"}
    v = surprise_pct(actual, consensus)
    return {"value": v, "warning": None if v is not None else "consensus nul ou absent : surprise % non définie"}


def pct_change(new: float | None, old: float | None) -> float | None:
    return surprise_pct(new, old)


def bps_change(new_ratio: float | None, old_ratio: float | None) -> float | None:
    return None if new_ratio is None or old_ratio is None else (new_ratio - old_ratio) * 10_000.0


def margin(numerator: float | None, denominator: float | None) -> float | None:
    return None if numerator is None or not denominator else numerator / denominator


def variance_line(name: str, actual: float | None, base: float | None, *, higher_is_better: bool = True,
                  material_pct: float = 10.0) -> dict:
    if actual is None or base is None:
        return {"name": name, "actual": actual, "base": base, "variance": None, "variance_pct": None,
                "favorable": None, "material": False}
    var = actual - base
    pct = pct_change(actual, base)
    fav = None if var == 0 else (var > 0) == higher_is_better
    return {"name": name, "actual": actual, "base": base, "variance": var, "variance_pct": pct, "favorable": fav,
            "material": pct is not None and abs(pct) >= material_pct}


def price_volume(actual_price: float, actual_volume: float, base_price: float, base_volume: float) -> dict:
    """Volume = (Vol réel − Vol base) × Prix base ; Prix = (Prix réel − Prix base) × Vol réel ; somme = variance totale."""
    volume = (actual_volume - base_volume) * base_price
    price = (actual_price - base_price) * actual_volume
    total = actual_price * actual_volume - base_price * base_volume
    return {"volume_effect": volume, "price_effect": price, "total": total, "check": abs(volume + price - total) < 1e-6}


@dataclass(frozen=True)
class Period:
    label: str
    revenue: float | None = None
    cost_of_revenue: float | None = None
    operating_income: float | None = None
    net_income: float | None = None
    eps_diluted: float | None = None
    operating_cash_flow: float | None = None
    capex: float | None = None

    @property
    def gross_profit(self) -> float | None:
        return None if self.revenue is None or self.cost_of_revenue is None else self.revenue - self.cost_of_revenue

    @property
    def free_cash_flow(self) -> float | None:
        return None if self.operating_cash_flow is None or self.capex is None else self.operating_cash_flow - abs(self.capex)


def compare_periods(cur: Period, base: Period, *, material_pct: float = 10.0) -> dict:
    """Compte de résultat / flux de trésorerie : variations et marges, période courante vs base (A-1 ou T-1)."""
    lines = [
        variance_line("Chiffre d'affaires", cur.revenue, base.revenue, material_pct=material_pct),
        variance_line("Marge brute ($)", cur.gross_profit, base.gross_profit, material_pct=material_pct),
        variance_line("Résultat opérationnel", cur.operating_income, base.operating_income, material_pct=material_pct),
        variance_line("Résultat net", cur.net_income, base.net_income, material_pct=material_pct),
        variance_line("BPA dilué", cur.eps_diluted, base.eps_diluted, material_pct=material_pct),
        variance_line("Free cash-flow", cur.free_cash_flow, base.free_cash_flow, material_pct=material_pct),
    ]
    margins = {
        "gross_margin_bps": bps_change(margin(cur.gross_profit, cur.revenue), margin(base.gross_profit, base.revenue)),
        "operating_margin_bps": bps_change(margin(cur.operating_income, cur.revenue), margin(base.operating_income, base.revenue)),
        "net_margin_bps": bps_change(margin(cur.net_income, cur.revenue), margin(base.net_income, base.revenue)),
    }
    return {"current": cur.label, "base": base.label, "lines": lines, "margins": margins}
