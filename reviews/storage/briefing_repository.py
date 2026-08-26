"""
Faits mesurés pour le résumé quotidien et le diagnostic de cause racine.

POURQUOI CE MODULE EXISTE SÉPARÉMENT DU MODÈLE
    Un LLM à qui l'on demande « quelle est la cause ? » en lui donnant des avis
    bruts produit une hypothèse plausible et invérifiable. À qui l'on donne
    « 78 % des plaintes portent sur un seul aspect, 91 % viennent d'un seul
    pays, 84 % sont tombées le même jour, et cet aspect était absent la semaine
    précédente », il n'a plus à deviner : il n'a qu'à nommer ce que les chiffres
    disent déjà.

    Tout ce qui SÉPARE deux explications concurrentes est donc calculé ici, en
    SQL, de façon déterministe. Le modèle ne reçoit que des faits, et son
    travail se réduit à les formuler — c'est la même règle que celle qui régit
    déjà `llm/insights.py`, appliquée à une question plus difficile.

LES QUATRE CONCENTRATIONS, ET CE QU'ELLES DISTINGUENT
    aspect       Un motif dominant (panne, facturation) ou un mécontentement
                 diffus ? Départage « incident » et « insatisfaction de fond ».
    géographique Un pays / une filiale, ou tout le périmètre ? Départage
                 « panne locale » et « décision groupe ».
    temporelle   Tout le même jour, ou étalé ? Départage « incident » et
                 « dégradation chronique ».
    source       Une seule source, ou plusieurs ? GARDE-FOU : une flambée visible
                 sur une seule plateforme est d'abord suspecte d'être un artefact
                 de collecte — un backfill, une source qui vient d'être activée —
                 avant d'être un problème réseau. Sans ce signal, le modèle
                 conclurait à une panne là où il n'y a qu'un changement de
                 périmètre de collecte.

CE QUE CE MODULE NE PEUT PAS FAIRE, ET IL FAUT LE SAVOIR
    Descendre sous le PAYS. Le corpus ne porte de lieu précis
    (`reviews.target_name`) que sur 40 lignes sur 16 147, toutes issues de
    Google Maps. Un diagnostic du type « panne dans la zone de Lagos » n'est
    donc pas soutenable par les données : la maille la plus fine réellement
    disponible est la filiale, et c'est celle que ces requêtes rendent.
"""

import logging
from dataclasses import replace
from typing import Any, Optional

from reviews.storage.db import Database
from reviews.storage.filters import (
    ASPECTS,
    BOTH_SIDES,
    CUSTOMER,
    ENRICHED,
    StatsFilter,
)

logger = logging.getLogger(__name__)

#: Les motifs et aspects sont volontairement lus SANS le filtre `comparable`.
#:
#: C'est la règle posée par la migration 007 et reprise telle quelle : HelloPeter
#: et Reddit sortent des TAUX comparés entre filiales, mais restent la meilleure
#: matière du corpus pour savoir DE QUOI les gens se plaignent (886 caractères
#: par avis contre 59 sur Google Play). Les écarter ici viderait le diagnostic de
#: sa substance.
#:
#: Le biais que cela introduit n'est pas caché au modèle : il lui est transmis
#: explicitement par la concentration par source.
#:
#: Le type de source est imposé par `where(source_kind=CUSTOMER)` et NON répété
#: ici : poser la clause deux fois laisserait le filtre de l'utilisateur en
#: ajouter une contradictoire (`source_kind = 'press'`), et le diagnostic
#: rendrait alors zéro ligne sans qu'aucune erreur ne l'explique.
_NEGATIFS = "t.polarity = %s"


class BriefingRepository:
    """Agrégats destinés au résumé et au diagnostic. Aucune écriture."""

    def __init__(self, db: Database):
        self.db = db

    def _aspects_negatifs(
        self, f: StatsFilter, window: Optional[tuple] = None
    ) -> tuple[str, list[Any]]:
        """Périmètre commun aux quatre lectures de `v_review_aspects`.

        Rassemble ce que chacune répétait — le filtre, la polarité négative — et
        y ajoute l'écart des aspects qui contredisent le côté regardé
        (migration 020). Un avis mixte reste dans le périmètre du service, mais
        son aspect applicatif n'a rien à faire dans un DIAGNOSTIC de service :
        sans cet écart, le briefing concluait « les plaintes portent sur les
        bugs de l'application » sur une filiale signalée pour son service.

        Factorisé plutôt que recopié quatre fois, parce que l'oubli d'une seule
        des quatre ne lèverait rien : elle rendrait juste un motif dominant
        différent de celui que le reste du briefing commente.
        """
        where, params = f.where(ASPECTS, window=window, source_kind=CUSTOMER)
        scope_clause, scope_params = f.aspect_scope_clause()
        return f"{where} AND {_NEGATIFS}{scope_clause}", params + ["negative"] + scope_params

    # ------------------------------------------------------------- Résumé

    def volumes(self, f: StatsFilter) -> dict:
        """Volumétrie de la fenêtre : combien d'avis, combien de négatifs, d'où.

        Sert d'ancrage au résumé : sans elle, le modèle énoncerait des motifs
        sans jamais dire sur combien d'avis ils reposent.
        """
        where, params = f.where(ENRICHED, source_kind=CUSTOMER)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT COUNT(*)                                        AS avis,
                       COUNT(*) FILTER (WHERE v.sentiment = 'negative') AS negatifs,
                       COUNT(DISTINCT v.subsidiary_id)                  AS filiales,
                       COUNT(DISTINCT v.iso2)                           AS pays
                FROM v_reviews_enriched v
                {where}
                """,
                params,
            )
            total = dict(cur.fetchone() or {})

            cur.execute(
                f"""
                SELECT v.source_code AS source, COUNT(*) AS avis,
                       COUNT(*) FILTER (WHERE v.sentiment = 'negative') AS negatifs
                FROM v_reviews_enriched v
                {where}
                GROUP BY 1
                ORDER BY 2 DESC
                """,
                params,
            )
            total["par_source"] = [dict(r) for r in cur.fetchall()]
        return total

    def pain_points(self, f: StatsFilter, limit: int = 8) -> list[dict]:
        """Principaux motifs d'insatisfaction, croisés PAYS x ASPECT.

        Le croisement est le cœur du résumé demandé : « lenteur d'Internet au
        Nigeria, recharge au Ghana, application au Maroc » est un croisement
        pays x aspect, pas un classement d'aspects ni un classement de pays.
        L'un sans l'autre ne produit que des généralités.

        `nb_filiales` accompagne chaque ligne parce qu'il distingue un motif
        SYSTÉMIQUE d'un motif LOCAL — la même distinction que rend `themes()`,
        et celle qui rend un motif actionnable ou non.
        """
        where, params = self._aspects_negatifs(f)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT t.iso2, t.country, t.aspect,
                       COUNT(*)                        AS avis,
                       COUNT(DISTINCT t.subsidiary_id) AS nb_filiales
                FROM v_review_aspects t
                {where}
                GROUP BY 1, 2, 3
                ORDER BY avis DESC
                LIMIT %s
                """,
                params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]

    # ---------------------------------------------------------- Diagnostic

    def signals(self, f: StatsFilter) -> dict:
        """Les quatre concentrations, plus la nouveauté du motif dominant.

        C'est ce bloc qui transforme le diagnostic en lecture de faits plutôt
        qu'en conjecture. Chaque part est rendue EN POURCENTAGE du total des
        plaintes de la fenêtre, pour que le modèle n'ait aucune division à
        faire — donc aucun chiffre à inventer.
        """
        aspects = self._distribution_aspects(f)
        total_aspects = sum(row["avis"] for row in aspects) or 0

        signaux: dict[str, Any] = {
            "aspect": self._concentration(aspects, "aspect", total_aspects),
            "geographique": self._concentration(
                self._distribution(f, "t.country", ASPECTS), "cle", None
            ),
            "filiale": self._concentration(
                self._distribution(f, "t.subsidiary", ASPECTS), "cle", None
            ),
            "source": self._concentration(
                self._distribution_sources(f), "cle", None
            ),
            "temporelle": self._concentration(
                self._distribution(
                    f, "to_char(t.occurred_at, 'YYYY-MM-DD')", ASPECTS
                ),
                "cle",
                None,
            ),
        }

        # NOUVEAUTÉ — le motif dominant existait-il déjà la fenêtre d'avant ?
        #
        # C'est le signal qui sépare le mieux un INCIDENT d'un problème
        # CHRONIQUE, et aucune des concentrations ne le porte : un motif peut
        # être très concentré et parfaitement habituel.
        dominant = signaux["aspect"].get("principal")
        if dominant:
            signaux["anteriorite"] = self._anteriorite(f, dominant)
        return signaux

    def _distribution_aspects(self, f: StatsFilter) -> list[dict]:
        where, params = self._aspects_negatifs(f)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT t.aspect, COUNT(*) AS avis
                FROM v_review_aspects t
                {where}
                GROUP BY 1 ORDER BY 2 DESC
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def _distribution(self, f: StatsFilter, expression: str, cols) -> list[dict]:
        """Répartition des plaintes selon une expression de regroupement.

        `expression` est un FRAGMENT SQL écrit en dur par l'appelant, jamais une
        donnée d'utilisateur — même règle que `FilterColumns`. Les valeurs
        filtrées, elles, restent des paramètres liés.
        """
        where, params = f.where(cols, source_kind=CUSTOMER)
        scope_clause, scope_params = f.aspect_scope_clause()
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT {expression} AS cle, COUNT(*) AS avis
                FROM v_review_aspects t
                {where} AND {_NEGATIFS}{scope_clause}
                GROUP BY 1 ORDER BY 2 DESC
                """,
                params + ["negative"] + scope_params,
            )
            return [dict(r) for r in cur.fetchall()]

    def _distribution_sources(self, f: StatsFilter) -> list[dict]:
        """Répartition par source, lue sur les AVIS et non sur les aspects.

        Un avis porte jusqu'à trois aspects négatifs : compter les sources sur
        la vue des aspects surpondérerait les sources aux avis les plus longs —
        précisément HelloPeter et Reddit. Le garde-fou anti-artefact serait
        alors faussé par le biais qu'il sert à détecter.
        """
        where, params = f.where(ENRICHED, source_kind=CUSTOMER)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT v.source_code AS cle, COUNT(*) AS avis
                FROM v_reviews_enriched v
                {where} AND v.sentiment = 'negative'
                GROUP BY 1 ORDER BY 2 DESC
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def _anteriorite(self, f: StatsFilter, aspect: str) -> dict:
        """Volume du motif dominant sur la fenêtre PRÉCÉDENTE, de durée égale."""
        where, params = self._aspects_negatifs(f, window=f.previous_window())
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) FILTER (WHERE t.aspect = %s) AS avis_motif,
                       COUNT(*)                              AS avis_total
                FROM v_review_aspects t
                {where}
                """,
                [aspect] + params,
            )
            row = dict(cur.fetchone() or {})
        avant = int(row.get("avis_motif") or 0)
        return {
            "motif": aspect,
            "avis_periode_precedente": avant,
            "total_periode_precedente": int(row.get("avis_total") or 0),
            # Rendu explicitement plutôt que laissé à déduire : « nouveau » est
            # l'information qui change le diagnostic, et une comparaison de deux
            # entiers est exactement le genre de calcul qu'un modèle rate.
            "nouveau": avant == 0,
        }

    @staticmethod
    def _concentration(
        rows: list[dict], key: str, total: Optional[int]
    ) -> dict:
        """Part du premier groupe, et nombre de groupes distincts.

        Une part élevée sur peu de groupes = phénomène concentré, donc
        localisable. Une part faible sur beaucoup de groupes = phénomène diffus,
        qu'aucune action ponctuelle ne traitera.
        """
        somme = total if total is not None else sum(r["avis"] for r in rows)
        if not rows or not somme:
            return {"principal": None, "part": None, "groupes": 0, "top": []}
        premier = rows[0]
        return {
            "principal": premier[key],
            "part": round(100.0 * premier["avis"] / somme, 1),
            "groupes": len(rows),
            "top": [
                {"cle": r[key], "avis": r["avis"],
                 "part": round(100.0 * r["avis"] / somme, 1)}
                for r in rows[:5]
            ],
        }

    def verbatims_for_aspect(
        self, f: StatsFilter, aspect: str, limit: int = 6, chars: int = 260
    ) -> list[str]:
        """Extraits portant PRÉCISÉMENT le motif dominant.

        Les verbatims génériques du périmètre ne servent à rien ici : sur une
        fenêtre où 78 % des plaintes visent la facturation, un échantillon
        aléatoire ramènerait surtout autre chose, et le modèle illustrerait son
        diagnostic avec des exemples qui le contredisent.

        DEUX PASSES, comme `StatsRepository.verbatims`. L'ancrage sur l'aspect
        ne suffit pas : un avis mixte porte bien `facturation_prix`, mais son
        texte peut consacrer trois lignes sur quatre à un bug de connexion. On
        privilégie donc les avis qui ne parlent que du service, et on ne
        complète avec les mixtes que si les premiers manquent — un diagnostic
        sans aucune citation serait pire qu'une citation imparfaite.
        """
        extraits = self._extraits_aspect(
            replace(f, about_strict=True), aspect, limit, chars
        )
        if len(extraits) < limit and f.about != BOTH_SIDES:
            vus = set(extraits)
            complement = self._extraits_aspect(
                replace(f, about_strict=False), aspect, limit, chars
            )
            extraits += [e for e in complement if e not in vus]
        return extraits[:limit]

    def _extraits_aspect(
        self, f: StatsFilter, aspect: str, limit: int, chars: int
    ) -> list[str]:
        """Une passe de `verbatims_for_aspect`."""
        where, params = f.where(ENRICHED, source_kind=CUSTOMER)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT LEFT(v.text, %s) AS extrait
                FROM v_reviews_enriched v
                {where}
                  AND v.sentiment = 'negative'
                  AND %s = ANY(v.neg_aspects)
                  AND length(btrim(v.text)) >= 40
                ORDER BY COALESCE(v.created_at, v.collected_at) DESC
                LIMIT %s
                """,
                [chars] + params + [aspect, limit],
            )
            return [
                " ".join((r["extrait"] or "").split())
                for r in cur.fetchall()
                if (r["extrait"] or "").strip()
            ]
