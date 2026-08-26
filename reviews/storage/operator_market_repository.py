"""
Indicateurs de marché PAR OPÉRATEUR : écriture idempotente et lectures.

POURQUOI LA RÉSOLUTION `subsidiary_id` SE FAIT ICI ET PAS DANS LE COLLECTEUR
    `NccNigeriaCollector` ne connaît pas le modèle dimensionnel — voir sa
    documentation. Il rend `operator_code` + `iso2`, exactement ce que rendrait
    n'importe quel futur collecteur régulateur ; c'est ici, au seul endroit qui
    parle à la base, que ce couple est résolu en `subsidiary_id`.

POURQUOI LE DÉDOUBLONNAGE EN PYTHON, ENCORE
    Même piège que `MarketRepository.upsert` (voir sa documentation et le test
    de régression associé) : `ON CONFLICT DO UPDATE` refuse de toucher deux
    fois la même ligne dans une seule commande. Une collecte NCC rejoue
    généralement les 12 derniers mois à chaque passage (la source ne garde
    qu'une fenêtre glissante) ; si un jour deux lots se recouvrent dans le même
    appel, dédoublonner avant l'envoi reste une condition de survie du lot.
"""

import logging
from datetime import date
from typing import Any, Optional

from psycopg2.extras import execute_values

from reviews.storage.db import Database

logger = logging.getLogger(__name__)


class OperatorMarketRepository:
    """Écriture et lecture des indicateurs de marché par opérateur."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- Écriture

    def upsert(self, lignes: list[dict]) -> int:
        """Insère ou met à jour des indicateurs par opérateur.

        `lignes` attend : operator_code, iso2, metric, period (str AAAA-MM-JJ),
        frequency, value, source, source_url.

        Une ligne dont le couple (operator_code, iso2) ne correspond à aucune
        filiale connue est écartée et journalisée — jamais levée : un
        opérateur mal orthographié ou disparu ne doit pas faire échouer les
        autres.
        """
        if not lignes:
            return 0

        resolues = self._resoudre_filiales(
            {(l["operator_code"], l["iso2"]) for l in lignes}
        )

        # DÉDOUBLONNAGE OBLIGATOIRE AVANT L'ENVOI — voir la documentation de
        # ce fichier. Le dernier arrivé fait foi.
        uniques: dict[tuple, tuple] = {}
        ecartees = 0
        for l in lignes:
            subsidiary_id = resolues.get((l["operator_code"], l["iso2"]))
            if subsidiary_id is None:
                ecartees += 1
                continue
            cle = (
                subsidiary_id,
                str(l["metric"])[:40],
                str(l["period"]),
                str(l["source"])[:40],
            )
            uniques[cle] = (*cle[:3], str(l["frequency"]), float(l["value"]),
                             cle[3], l.get("source_url"))

        if ecartees:
            logger.warning(
                "Marché opérateur : %d ligne(s) écartée(s), filiale introuvable",
                ecartees,
            )
        doublons = len(lignes) - ecartees - len(uniques)
        if doublons:
            logger.info(
                "Marché opérateur : %d doublon(s) de clé écarté(s) avant écriture",
                doublons,
            )

        valeurs = list(uniques.values())
        if not valeurs:
            return 0

        with self.db.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO operator_market_indicators
                    (subsidiary_id, metric, period, frequency, value, source, source_url)
                VALUES %s
                ON CONFLICT (subsidiary_id, metric, period, source)
                DO UPDATE SET value = EXCLUDED.value,
                              frequency = EXCLUDED.frequency,
                              source_url = EXCLUDED.source_url,
                              collected_at = now()
                """,
                valeurs,
            )
        return len(valeurs)

    def _resoudre_filiales(self, couples: set[tuple[str, str]]) -> dict[tuple[str, str], int]:
        """(operator_code, iso2) -> subsidiary_id, pour les couples demandés."""
        if not couples:
            return {}
        with self.db.cursor() as cur:
            cur.execute(
                """
                SELECT o.code, c.iso2, s.subsidiary_id
                FROM dim_subsidiary s
                JOIN dim_operator o ON o.operator_id = s.operator_id
                JOIN dim_country  c ON c.country_id  = s.country_id
                """
            )
            toutes = {(code, iso2): sid for code, iso2, sid in cur.fetchall()}
        return {c: toutes[c] for c in couples if c in toutes}

    # -------------------------------------------------------------- Lecture

    def latest_by_subsidiary(
        self, metric: Optional[str] = None, recent_only: bool = True,
    ) -> list[dict]:
        """Dernière valeur connue de chaque (filiale, indicateur).

        Une ligne par filiale — la forme du tableau comparatif « par
        opérateur », symétrique de `MarketRepository.latest_by_country`.

        LA VARIATION EST CALCULÉE ICI, jamais côté écran ni par un modèle —
        même règle que `MarketRepository.latest`.

        `recent_only` (vrai par défaut) NE GARDE QUE LES FILIALES DONT LA
        DERNIÈRE MESURE TOMBE DANS L'ANNÉE EN COURS — demandé le 24 août
        2026 : le dashboard ne doit plus montrer QUE des opérateurs à jour,
        jamais un chiffre 2024 ou 2025 mêlé aux chiffres 2026. Filtré ici,
        après le DISTINCT ON (qui prend déjà la période la plus récente par
        filiale), pas dans le SQL — la ligne écartée reste en base et reste
        interrogeable par année si besoin, seul l'AFFICHAGE se resserre.
        L'année de référence est CALCULÉE (`date.today().year`), jamais
        écrite en dur : un « 2026 » figé se remettrait à mentir dès janvier
        2027, exactement le défaut qu'on retire ici pour la source pays.
        """
        clauses = ["1=1"]
        params: list[Any] = []
        if metric:
            clauses.append("m.metric = %s")
            params.append(metric)

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT DISTINCT ON (m.subsidiary_id, m.metric)
                       m.subsidiary_id, sub.name AS subsidiary,
                       op.operator_id, op.name AS operator,
                       co.iso2, co.name AS country,
                       m.metric, m.period, m.frequency, m.value,
                       m.source, m.source_url,
                       LAG(m.value) OVER (
                           PARTITION BY m.subsidiary_id, m.metric ORDER BY m.period
                       ) AS valeur_precedente,
                       LAG(m.period) OVER (
                           PARTITION BY m.subsidiary_id, m.metric ORDER BY m.period
                       ) AS periode_precedente
                FROM operator_market_indicators m
                JOIN dim_subsidiary sub ON sub.subsidiary_id = m.subsidiary_id
                JOIN dim_operator   op  ON op.operator_id    = sub.operator_id
                JOIN dim_country    co  ON co.country_id     = sub.country_id
                WHERE {' AND '.join(clauses)}
                ORDER BY m.subsidiary_id, m.metric, m.period DESC
                """,
                params,
            )
            lignes = [dict(r) for r in cur.fetchall()]

        for l in lignes:
            avant = l.get("valeur_precedente")
            l["variation_pct"] = (
                round((l["value"] - avant) / avant * 100, 1)
                if avant not in (None, 0)
                else None
            )

        if recent_only:
            annee_courante = date.today().year
            lignes = [l for l in lignes if l["period"].year == annee_courante]

        return sorted(lignes, key=lambda l: (l["country"], l["operator"]))

    def for_subsidiary(self, subsidiary_id: int, metric: Optional[str] = None) -> list[dict]:
        """Série complète d'une filiale, la plus récente d'abord."""
        clauses = ["m.subsidiary_id = %s"]
        params: list[Any] = [subsidiary_id]
        if metric:
            clauses.append("m.metric = %s")
            params.append(metric)

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT m.metric, m.period, m.frequency, m.value,
                       m.source, m.source_url
                FROM operator_market_indicators m
                WHERE {' AND '.join(clauses)}
                ORDER BY m.metric, m.period DESC
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]
