"""
Cache des textes produits par le modèle, partagé par toutes les synthèses.

POURQUOI UN MODULE À PART
    Trois familles de textes passent par la table `llm_insights` — explications
    d'écart, résumés de période, diagnostics. Elles n'ont ni le même prompt ni
    le même contexte, mais elles ont exactement la même question à résoudre :
    « ce périmètre a-t-il déjà été payé ? » Recopiée dans chaque service, la
    réponse finirait par diverger, et une divergence ici a une conséquence
    précise et grave : un texte juste affiché sous des chiffres qui ne sont pas
    les siens.

L'EMPREINTE PORTE LE PÉRIMÈTRE COMPLET
    Filtres, fenêtre, entités, version du prompt. Deux périmètres différents ne
    partagent donc jamais une phrase. `sort_keys` et le tri des entités rendent
    l'empreinte indépendante de l'ordre d'énumération : le même écran rouvert
    avec deux filiales sélectionnées dans l'autre sens ne repaie pas un appel.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from reviews.storage.db import Database

logger = logging.getLogger(__name__)


@dataclass
class CachedText:
    """Un texte produit par le modèle, tel qu'il est rendu au dashboard."""

    text: str
    kind: str
    cached: bool
    reliability: Optional[str] = None
    payload: Optional[dict] = None
    model: Optional[str] = None
    created_at: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "available": True,
            "text": self.text,
            "kind": self.kind,
            "cached": self.cached,
            "reliability": self.reliability,
            "payload": self.payload,
            "model": self.model,
            "created_at": self.created_at,
        }


def scope_hash(material: dict) -> str:
    """Empreinte stable d'un périmètre de question."""
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class InsightCache:
    """Lecture/écriture de `llm_insights`. Ne lève jamais.

    Un cache indisponible coûte un appel de plus, jamais une page en erreur :
    c'est la même règle que dans tout le reste de la couche IA, où une
    dépendance externe qui tombe ne doit pas faire tomber un écran.
    """

    def __init__(self, db: Database):
        self.db = db

    def read(self, kind: str, digest: str) -> Optional[CachedText]:
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    "SELECT text, payload, model, created_at FROM llm_insights "
                    "WHERE kind = %s AND scope_hash = %s",
                    (kind, digest),
                )
                row = cur.fetchone()
        except Exception:  # noqa: BLE001
            logger.warning("Cache des synthèses illisible.", exc_info=True)
            return None
        if not row:
            return None
        payload = row["payload"] or {}
        return CachedText(
            text=row["text"],
            kind=kind,
            cached=True,
            reliability=payload.get("_fiabilite"),
            payload=payload,
            model=row["model"],
            created_at=row["created_at"].isoformat() if row["created_at"] else None,
        )

    def write(
        self, kind: str, digest: str, scope: Any, entry: CachedText
    ) -> None:
        payload = dict(entry.payload or {})
        payload["_fiabilite"] = entry.reliability
        try:
            with self.db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO llm_insights
                        (kind, scope_hash, scope, payload, text, model)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    -- Une question déjà répondue par un appel concurrent ne
                    -- doit pas faire échouer celui-ci : les deux textes sont
                    -- équivalents, le premier arrivé fait foi.
                    ON CONFLICT (kind, scope_hash) DO NOTHING
                    """,
                    (
                        kind,
                        digest,
                        json.dumps(scope, default=str),
                        json.dumps(payload, ensure_ascii=False, default=str),
                        entry.text,
                        entry.model,
                    ),
                )
        except Exception:  # noqa: BLE001
            # Un cache non écrit coûte un appel de plus, pas une panne.
            logger.warning("Synthèse non mise en cache.", exc_info=True)
