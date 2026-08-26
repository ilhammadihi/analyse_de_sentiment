"""
Indicateurs de marché : écriture idempotente et lectures du dashboard.

POURQUOI L'ÉCRITURE EST UN UPSERT, ET NON UN INSERT
    La source publie des données annuelles qu'elle RÉVISE : une valeur 2024
    provisoire est corrigée quelques mois plus tard. Un collecteur qui insère
    créerait un doublon par passage ; un collecteur qui vide puis réinsère
    perdrait tout si l'appel suivant échoue. L'upsert sur la clé naturelle
    (pays, indicateur, unité, année, fournisseur) laisse la dernière valeur
    connue faire autorité sans jamais multiplier les lignes.
"""

import logging
from typing import Any, Optional

from psycopg2.extras import execute_values

from reviews.storage.db import Database

logger = logging.getLogger(__name__)


class MarketRepository:
    """Écriture et lecture des indicateurs de marché."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------- Écriture

    def upsert(self, lignes: list[dict]) -> int:
        """Insère ou met à jour des indicateurs. Renvoie le nombre traité.

        `lignes` attend : country_id, indicator, unit, year, value, provider.
        `source_url` est optionnel — les collectes automatiques (Banque
        Mondiale/UIT) n'ont pas de page individuelle à citer par mesure ;
        les saisies manuelles ponctuelles (presse, voir 026) en ont une, et
        c'est elle qui permet de justifier le chiffre à l'écran.
        """
        if not lignes:
            return 0

        # DÉDOUBLONNAGE OBLIGATOIRE AVANT L'ENVOI, et ce n'est pas une
        # précaution de confort : PostgreSQL refuse qu'un `ON CONFLICT DO
        # UPDATE` touche deux fois la même ligne dans une seule commande. Deux
        # lignes de même clé dans le lot font échouer LE LOT ENTIER, pas
        # seulement le doublon — 177 mesures perdues à cause d'une, sur la
        # première collecte réelle. Le dernier arrivé fait foi.
        uniques: dict[tuple, tuple] = {}
        for l in lignes:
            cle = (
                int(l["country_id"]),
                str(l["indicator"])[:40],
                str(l["unit"])[:20],
                int(l["year"]),
                str(l.get("provider") or "worldbank_itu")[:40],
            )
            uniques[cle] = (*cle[:4], float(l["value"]), cle[4], l.get("source_url"))

        ecartes = len(lignes) - len(uniques)
        if ecartes:
            logger.info(
                "Marché : %d doublon(s) de clé écarté(s) avant écriture", ecartes
            )
        valeurs = list(uniques.values())
        with self.db.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO market_indicators
                    (country_id, indicator, unit, year, value, provider, source_url)
                VALUES %s
                ON CONFLICT (country_id, indicator, unit, year, provider)
                DO UPDATE SET value = EXCLUDED.value,
                              source_url = EXCLUDED.source_url,
                              collected_at = now()
                """,
                valeurs,
            )
        return len(valeurs)

    # -------------------------------------------------------------- Lecture

    def for_country(self, iso2: str, indicators: Optional[list[str]] = None) -> list[dict]:
        """Indicateurs d'un pays, année la plus récente d'abord."""
        clauses = ["c.iso2 = %s"]
        params: list[Any] = [iso2.upper()]
        if indicators:
            clauses.append("m.indicator = ANY(%s)")
            params.append(list(indicators))

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT c.iso2, c.name AS country, m.indicator, m.unit,
                       m.year, m.value, m.provider
                FROM market_indicators m
                JOIN dim_country c ON c.country_id = m.country_id
                WHERE {' AND '.join(clauses)}
                ORDER BY m.indicator, m.unit, m.year DESC
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def latest(self, iso2: str) -> dict[str, dict]:
        """Dernière valeur connue de chaque (indicateur, unité) pour un pays.

        C'est la forme dont l'agent a besoin : il raisonne sur « la valeur la
        plus récente et celle d'avant », pas sur une série complète. La
        variation est calculée ICI et non par le modèle — même règle que
        partout ailleurs dans ce projet.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (m.indicator, m.unit)
                       m.indicator, m.unit, m.year, m.value,
                       m.provider, m.source_url,
                       LAG(m.value) OVER (
                           PARTITION BY m.indicator, m.unit ORDER BY m.year
                       ) AS valeur_precedente,
                       LAG(m.year) OVER (
                           PARTITION BY m.indicator, m.unit ORDER BY m.year
                       ) AS annee_precedente
                FROM market_indicators m
                JOIN dim_country c ON c.country_id = m.country_id
                WHERE c.iso2 = %s
                ORDER BY m.indicator, m.unit, m.year DESC
                """,
                (iso2.upper(),),
            )
            out: dict[str, dict] = {}
            for r in cur.fetchall():
                d = dict(r)
                avant = d.get("valeur_precedente")
                # La variation n'est calculée que si les deux points existent
                # ET que le point de départ n'est pas nul : une progression
                # « depuis zéro » n'a pas de pourcentage défendable.
                d["variation_pct"] = (
                    round((d["value"] - avant) / avant * 100, 1)
                    if avant not in (None, 0)
                    else None
                )
                out[f"{d['indicator']}|{d['unit']}"] = d
            return out

    def latest_by_country(self, indicators: list[str]) -> list[dict]:
        """Dernière valeur connue de chaque indicateur, pour TOUS les pays.

        Une ligne par pays, une colonne par indicateur — la forme d'un tableau
        comparatif et celle du croisement satisfaction × réseau.

        POURQUOI UNE SEULE REQUÊTE ET NON UNE PAR PAYS. L'écran compare 54
        pays : 54 allers-retours produiraient le même résultat en cinquante
        fois plus de temps, et le tableau s'afficherait par à-coups.

        L'ANNÉE EST RENDUE AVEC LA VALEUR, et ce n'est pas décoratif : la
        couverture 4G est connue pour 2024 sur les 54 pays, le trafic data pour
        34 seulement, et parfois sur une année plus ancienne. Comparer deux
        pays sans voir que l'un est mesuré en 2024 et l'autre en 2021 serait
        une comparaison fausse — l'écran doit pouvoir le signaler.
        """
        if not indicators:
            return []

        with self.db.cursor(dict_rows=True) as cur:
            # L'UNITÉ FAIT PARTIE DU `DISTINCT ON`, sans quoi `IT_CEL_SETS` —
            # qui existe en abonnements ET pour 100 habitants — verrait l'une
            # de ses deux mesures écraser l'autre au hasard de l'ordre de tri.
            cur.execute(
                """
                SELECT DISTINCT ON (c.iso2, m.indicator, m.unit)
                       c.iso2, c.name AS country, c.region,
                       m.indicator, m.unit, m.year, m.value,
                       m.provider, m.source_url
                FROM market_indicators m
                JOIN dim_country c ON c.country_id = m.country_id
                WHERE m.indicator = ANY(%s)
                ORDER BY c.iso2, m.indicator, m.unit, m.year DESC
                """,
                (list(indicators),),
            )
            lignes = cur.fetchall()

        pays: dict[str, dict] = {}
        for r in lignes:
            entree = pays.setdefault(
                r["iso2"],
                {"iso2": r["iso2"], "country": r["country"], "region": r["region"],
                 "indicators": {}},
            )
            # Clé « indicateur|unité », comme dans `latest()` : deux mesures du
            # même indicateur dans des unités différentes sont deux mesures.
            #
            # `provider`/`source_url` accompagnent CHAQUE mesure, pas le pays
            # entier : un pays peut porter à la fois un indicateur officiel
            # (Banque Mondiale/UIT) et un indicateur `press` plus récent —
            # voir 026. Sans ça, l'écran ne pourrait pas distinguer les deux.
            entree["indicators"][f"{r['indicator']}|{r['unit']}"] = {
                "indicator": r["indicator"], "unit": r["unit"],
                "year": r["year"], "value": r["value"],
                "provider": r["provider"], "source_url": r["source_url"],
            }
        return sorted(pays.values(), key=lambda p: p["country"])

    def coverage(self) -> list[dict]:
        """Combien d'indicateurs par pays, et jusqu'à quelle année.

        Sert au futur agent de qualité de données : un pays sans indicateur est
        un trou de couverture, exactement comme une filiale sans avis.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT c.iso2, c.name AS country,
                       COUNT(m.indicator)   AS indicateurs,
                       MAX(m.year)          AS derniere_annee
                FROM dim_country c
                LEFT JOIN market_indicators m ON m.country_id = c.country_id
                GROUP BY c.iso2, c.name
                ORDER BY indicateurs, c.name
                """
            )
            return [dict(r) for r in cur.fetchall()]
