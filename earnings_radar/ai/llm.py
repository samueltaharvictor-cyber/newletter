"""IA locale OPTIONNELLE (Ollama). Désactivée par défaut, jamais dans le chemin critique de détection.
Elle ne reçoit que des faits structurés, ne peut pas modifier un signal, et sa sortie est rejetée si elle contient un chiffre absent des faits."""
from __future__ import annotations

import json
from typing import Protocol

from .guard import verify_summary

SYSTEM_PROMPT = (
    "Tu rédiges une synthèse factuelle en français d'un communiqué de résultats. Utilise UNIQUEMENT les faits JSON fournis. "
    "N'invente aucun chiffre, ne donne aucune recommandation d'achat ou de vente, ne prédis pas le cours."
)


class LocalLLM(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class OllamaClient:
    """POST {base_url}/api/chat (stream=false, température 0). Non testé en réel dans le bac à sable."""

    def __init__(self, transport, base_url: str, model: str, timeout: float = 120.0):
        self.t, self.base, self.model, self.timeout = transport, base_url.rstrip("/"), model, timeout

    async def complete(self, system: str, user: str) -> str:
        r = await self.t.request("POST", f"{self.base}/api/chat", json_body={
            "model": self.model, "stream": False, "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}, timeout=self.timeout)
        if r.status >= 400:
            raise RuntimeError(f"Ollama HTTP {r.status}")
        return r.json()["message"]["content"]


async def guarded_summary(llm: LocalLLM, facts: dict) -> dict:
    """Retourne {'text', 'ok', 'unknown_numbers'} ; si le garde-fou échoue, le texte est écarté (None)."""
    text = (await llm.complete(SYSTEM_PROMPT, json.dumps(facts, ensure_ascii=False))).strip()
    res = verify_summary(text, facts)
    return {"text": text if res.ok else None, "ok": res.ok, "unknown_numbers": list(res.unknown)}
