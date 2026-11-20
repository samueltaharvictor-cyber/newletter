"""Retrouve et lit le communiqué de résultats (pièce EX-99) d'un dépôt 8-K de la SEC."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .analyze import html_to_text

_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)


def find_release_urls(index_html: str, base: str = "https://www.sec.gov") -> list[str]:
    """Liens des pièces de type EX-99 / EX-99.1 dans la page d'index d'un dépôt."""
    out: list[str] = []
    for row in _ROW.findall(index_html):
        if re.search(r">\s*EX-99(?:\.\d+)?\s*<", row, re.I):
            m = re.search(r'href="([^"]+)"', row, re.I)
            if m:
                href = m.group(1)
                if href.startswith("/ix?"):                     # visionneuse : le vrai document est dans ?doc=
                    href = parse_qs(urlsplit(href).query).get("doc", [href])[0]
                out.append(urljoin(base, href))
    return out


async def fetch_release_text(api, index_url: str, max_docs: int = 2) -> tuple[str, str] | None:
    """(texte du communiqué, son URL) ou None s'il n'y en a pas."""
    idx = await api.get_text(index_url)
    for url in find_release_urls(idx.text)[:max_docs]:
        resp = await api.get_text(url)
        text = html_to_text(resp.text)
        if len(text) > 200:
            return text, url
    return None
