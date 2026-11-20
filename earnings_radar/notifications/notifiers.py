"""Alertes : messages (3 étapes de V2 §11 — ici l'étape 1 + rappels + surveillance) et canaux ntfy / Telegram."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from ..app.serialize import SOURCE_LABELS
from ..core.domain import EarningsEvent
from ..core.marketcal import tri_time
from ..net.http import ApiClient


@dataclass(frozen=True)
class Alert:
    title: str
    body: str
    priority: str = "high"      # high | default
    url: str | None = None


class Notifier(Protocol):
    name: str
    async def send(self, alert: Alert) -> None: ...


def _tri(ts) -> str:
    t = tri_time(ts)
    return f"{t['et']} | {t['paris']}" if t else "—"


def build_alert(row: dict) -> Alert:
    who = row.get("ticker") or row.get("company_name") or "?"
    tag = {"simulated": "[TEST] ", "recovered": "[RATTRAPÉ] "}.get(row["detection_mode"], "")
    ref = row["published_at"] or row["provider_at"] or row["received_at"]
    lines = ["Résultats disponibles, comparaison au consensus en cours.",
             f"Source : {SOURCE_LABELS.get(row['source'], row['source'])} — confiance : {row['confidence']}",
             f"Publication : {_tri(ref)}"]
    if not row["scheduled"] and row["detection_mode"] != "simulated":
        lines.insert(1, "⚠ Publication NON PRÉVUE au calendrier.")
    if row["confidence"] == "headline":
        lines.append("À confirmer par le dépôt 8-K de la SEC.")
    lines.append("Signal : pas encore validé.")
    return Alert(f"{tag}EARNINGS DÉTECTÉ — {who}", "\n".join(lines), "high", row.get("url"))


def build_reminder(ev: EarningsEvent, lead_min: int) -> Alert:
    return Alert(f"Dans {lead_min} min — {ev.ticker}",
                 f"Publication annoncée : {_tri(ev.scheduled_at)}\n"
                 f"{'Heure CONFIRMÉE' if ev.time_confirmed else 'Heure NON confirmée (estimation de la source)'}.", "default")


def build_watchdog(provider: str, capability: str, stale_s: float, recovered: bool = False) -> Alert:
    if recovered:
        return Alert(f"Surveillance rétablie — {provider}/{capability}", "Les données reviennent normalement.", "default")
    return Alert(f"⚠ Flux silencieux — {provider}/{capability}",
                 f"Aucune réponse valide depuis {int(stale_s)} s pendant les heures d'ouverture : la détection est dégradée.", "high")


class NtfyNotifier:
    """Publication JSON sur le serveur ntfy (UTF-8 géré ; pas de caractères accentués dans les en-têtes HTTP)."""
    name = "ntfy"

    def __init__(self, api: ApiClient, topic_url: str):
        u = urlparse(topic_url)
        self.api, self.base, self.topic = api, f"{u.scheme}://{u.netloc}", u.path.strip("/")

    async def send(self, alert: Alert) -> None:
        body = {"topic": self.topic, "title": alert.title, "message": alert.body, "priority": 4 if alert.priority == "high" else 3,
                "tags": ["chart_with_upwards_trend"]}
        if alert.url:
            body["click"] = alert.url
        await self.api.post_json(self.base, body)


class TelegramNotifier:
    name = "telegram"

    def __init__(self, api: ApiClient, token: str, chat_id: str):
        self.api, self.token, self.chat_id = api, token, chat_id

    async def send(self, alert: Alert) -> None:
        await self.api.post_json(f"https://api.telegram.org/bot{self.token}/sendMessage", {
            "chat_id": self.chat_id, "text": f"{alert.title}\n{alert.body}" + (f"\n{alert.url}" if alert.url else ""),
            "disable_web_page_preview": True})


def build_notifiers(settings, transport, health=None) -> list[Notifier]:
    out: list[Notifier] = []
    if settings.ntfy_url:
        out.append(NtfyNotifier(ApiClient("ntfy", "notify", transport, retries=1, timeout=8, health=health), settings.ntfy_url))
    if settings.telegram_bot_token and settings.telegram_chat_id:
        out.append(TelegramNotifier(ApiClient("telegram", "notify", transport, retries=1, timeout=8, health=health),
                                    settings.telegram_bot_token, settings.telegram_chat_id))
    return out
