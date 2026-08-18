"""
Agrégats du dashboard.

Séparé de `repository.py` — qui porte le chemin d'écriture (insertion des avis,
cycle de vie des runs, alertes) — parce que c'est une préoccupation distincte :
ici, on ne fait que LIRE, et chaque requête doit produire des chiffres
cohérents entre eux d'un écran à l'autre. C'est ce contrat de cohérence que le
module rend explicite, à travers deux mécanismes uniques :

  - `_MEASURES`, seule définition des indicateurs de satisfaction ;
  - `StatsFilter`, seule traduction du périmètre en SQL (voir filters.py).

Aucune méthode ne réécrit un filtre ni un taux à la main. C'est la règle qui
empêche le retour du défaut d'origine : le même indicateur affiché à 8,3 % sur
un écran et 16,7 % sur un autre, faute d'un dénominateur commun.
"""

import logging
from dataclasses import replace
from typing import Any, Optional

from reviews.config import get_settings
from reviews.domain.aspects import OTHER, label as aspect_label
from reviews.domain.models import SourceEnum
from reviews.storage.db import Database
from reviews.storage.filters import (
    APP,
    BOTH_SIDES,
    CUSTOMER,
    ENRICHED,
    OPERATOR,
    PRESS,
    TERMS,
    StatsFilter,
    resolve_level,
    safe_granularity,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# Mesures partagées
# ===========================================================================

#: Bloc de mesures utilisé par TOUS les agrégats dimensionnels.
#:
#: Défini une seule fois, et c'est là l'essentiel : c'est ce qui garantit que
#: « part de négatifs » désigne la même chose partout. Avant ce bloc, la vue
#: d'ensemble divisait les avis négatifs par le total presse comprise, là où les
#: autres écrans divisaient par les seuls avis clients — le même indicateur
#: s'affichait à 8,3 % ici et 16,7 % là.
#:
#: RÈGLE : la satisfaction (note, sentiment, score) ne se calcule QUE sur
#: `customer_review`. La presse est comptée à part et n'entre dans aucune
#: moyenne — elle est neutre à 90 % et deux fois plus volumineuse que les avis
#: clients, elle diviserait par deux tout taux calculé sur le total.
#:
#: SECONDE CONDITION, ajoutée avec HelloPeter (migration 007) : `source_comparable`.
#:
#: Être de la voix client ne suffit pas à être comparable. HelloPeter est une
#: plateforme de PLAINTES — on n'y va que quand ça a mal tourné — et affiche
#: 97,7 % de négatifs là où l'App Store est à 60,7 % et Google Play à 37,0 %.
#: Mélangée aux autres, elle déplaçait la part de négatifs des filiales
#: sud-africaines de +1,5 point (Telkom) à +11,7 (Vodacom) : un décalage qui
#: dépend du rapport de volumes, donc qui bouge à chaque run et qu'aucun
#: coefficient fixe ne peut corriger. Elle avait déclenché deux fausses alertes
#: « pic de mécontentement » dès son premier run.
#:
#: Ces avis ne sont PAS perdus : ils restent en base, alimentent les verbatims,
#: les motifs et la couche sémantique — où ils sont de loin la meilleure
#: matière du corpus (886 caractères en moyenne contre 59 sur Google Play) — et
#: leur volume est rendu explicitement dans `avis_hors_comparaison`.
#:
#: TROISIÈME CONDITION, ET ELLE N'EST PAS ICI : l'objet de l'avis (migration
#: 019). Depuis que les boutiques d'applications pèsent 83 % du corpus, il faut
#: aussi séparer ce qui juge le SERVICE de ce qui juge l'APPLICATION — sans quoi
#: une mise à jour ratée déclenche un « pic de mécontentement » sur une filiale
#: dont le réseau n'a pas bougé.
#:
#: Cette condition vit dans `StatsFilter.about`, dont le défaut est `operator`,
#: et NON dans ce bloc. La raison est qu'elle doit rester RETOURNABLE : inscrite
#: ici, elle se cumulerait au filtre et `about=app` ne laisserait passer que les
#: 2 006 avis mixtes. Portée par le filtre, les mêmes mesures servent les deux
#: satisfactions, chacune sur sa moitié du corpus.
#:
#: Le fragment suppose la vue `v_reviews_enriched` aliasée `v`.
_CLIENTS = "v.source_kind = 'customer_review' AND v.source_comparable"

#: Avis clients exigés pour qu'un TAUX soit publiable.
#:
#: MESURÉ : sur 132 filiales, 9 seulement atteignent 10 avis clients sur sept
#: jours, et 23 sur trente. Autrement dit, la majorité des taux affichés
#: reposaient sur une poignée d'avis — « 75 % de négatifs » calculé sur quatre
#: avis est un mensonge statistique présenté avec l'autorité d'un graphique.
#:
#: 30 plutôt que 10 : à dix avis, un seul avis fait bouger le taux de dix
#: points, soit l'amplitude du seuil d'alerte lui-même. À trente, un avis pèse
#: 3,3 points.
#:
#: Le seuil vit ICI et non dans le dashboard : c'est la même règle que pour les
#: taux eux-mêmes. Recalculée dans chaque composant, elle finirait par diverger
#: d'un écran à l'autre — le défaut d'origine que `_MEASURES` a supprimé.
RELIABILITY_MIN_REVIEWS = 30

#: Composition des sources, en une colonne JSON.
#:
#: POURQUOI ELLE EST INDISPENSABLE, et pas un simple confort
#:     Les sources ne notent pas de la même façon. Mesuré sur le corpus réel :
#:     App Store 60,7 % de négatifs (note 2,31), Google Play 36,8 % (3,28),
#:     Google Maps 31,5 % (3,49). Vingt-neuf points d'écart.
#:
#:     Sur les 65 filiales dépassant 30 avis, la corrélation entre « part
#:     d'App Store dans le corpus » et « taux de négatif » atteint 0,60. Une
#:     filiale vue surtout par l'App Store paraît donc bien pire qu'une filiale
#:     équivalente vue par Google Maps, pour une raison étrangère à la qualité
#:     de son service.
#:
#:     Le tableau de bord existe pour comparer des filiales ; son indicateur
#:     principal est contaminé par la composition des corpus, et rien à l'écran
#:     ne le laissait voir. Publier la composition à côté du taux est le
#:     minimum : c'est ce qui permet au lecteur de savoir qu'il compare deux
#:     mesures faites avec des instruments différents.
#:
#: `jsonb_strip_nulls` + `NULLIF(..., 0)` : une source absente disparaît de
#: l'objet au lieu d'y figurer à zéro, ce qui rend la charge utile lisible et
#: évite huit clés inutiles sur chacune des 132 lignes d'un classement.
_COMPOSITION = ",\n".join(
    f"        '{s.value}', NULLIF(COUNT(*) FILTER "
    f"(WHERE {_CLIENTS} AND v.source_code = '{s.value}'), 0)"
    for s in SourceEnum
)

_MEASURES = f"""
    COUNT(*)                                                    AS total,
    COUNT(*) FILTER (WHERE {_CLIENTS})                          AS avis_clients,
    -- Rendu explicitement plutôt que passé sous silence : sans cette mesure,
    -- 220 avis disparaîtraient de tous les écrans sans que rien ne l'indique,
    -- et l'écart entre `total` et `avis_clients + articles_presse` serait
    -- inexplicable pour qui lit le dashboard.
    COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                       AND NOT v.source_comparable)             AS avis_hors_comparaison,
    COUNT(*) FILTER (WHERE v.source_kind = 'press')             AS articles_presse,
    COUNT(*) FILTER (WHERE {_CLIENTS}
                       AND v.sentiment = 'positive')            AS positifs,
    COUNT(*) FILTER (WHERE {_CLIENTS}
                       AND v.sentiment = 'neutral')             AS neutres,
    COUNT(*) FILTER (WHERE {_CLIENTS}
                       AND v.sentiment = 'negative')            AS negatifs,
    ROUND((AVG(v.rating) FILTER (WHERE {_CLIENTS}))::numeric, 2)
                                                                AS note_moyenne,
    -- Score continu moyen sur [-1, 1]. Plus fin que la part de négatifs :
    -- celle-ci compte de la même façon un avis à peine négatif (-0,18) et un
    -- avis très négatif (-0,95), que le score distingue.
    ROUND((AVG(v.sentiment_score) FILTER (WHERE {_CLIENTS}))::numeric, 3)
                                                                AS score_moyen,
    -- Les taux sont calculés EN SQL, pas dans le dashboard : un taux recalculé
    -- dans chaque composant finit toujours par diverger d'un écran à l'autre.
    -- NULLIF : un périmètre sans aucun avis client renvoie NULL, jamais 0 —
    -- « pas d'information » et « aucun négatif » ne doivent pas se lire pareil.
    ROUND((100.0 * COUNT(*) FILTER (WHERE {_CLIENTS}
                                      AND v.sentiment = 'negative')
           / NULLIF(COUNT(*) FILTER (WHERE {_CLIENTS}), 0))::numeric, 1)
                                                                AS part_negatifs,
    ROUND((100.0 * COUNT(*) FILTER (WHERE {_CLIENTS}
                                      AND v.sentiment = 'positive')
           / NULLIF(COUNT(*) FILTER (WHERE {_CLIENTS}), 0))::numeric, 1)
                                                                AS part_positifs,

    -- FIABILITÉ — le taux ci-dessus est-il publiable ?
    --
    -- Calculée en SQL et non dans le dashboard, pour la raison qui a présidé à
    -- tout ce bloc : une règle recopiée dans chaque composant finit par
    -- diverger d'un écran à l'autre. Le taux reste renvoyé quoi qu'il arrive —
    -- l'écran décide de l'afficher ou de lui préférer le compte brut, mais il
    -- décide sur un verdict unique.
    (COUNT(*) FILTER (WHERE {_CLIENTS}) >= {RELIABILITY_MIN_REVIEWS})
                                                                AS fiable,

    -- FRAÎCHEUR — de quand date le dernier avis client de ce périmètre ?
    --
    -- Mesuré : 2 filiales sur 132 ont un avis de moins de 24 h, 39 en ont un de
    -- moins de sept jours. Sans cette date, rien à l'écran ne distingue une
    -- filiale CALME d'une filiale dont la collecte est CASSÉE — les deux
    -- affichent le même vide, et l'une des deux appelle une intervention.
    MAX({ENRICHED.occurred_at}) FILTER (WHERE {_CLIENTS})        AS dernier_avis,

    -- COMPOSITION DES SOURCES — avec quel instrument ce taux a-t-il été mesuré ?
    jsonb_strip_nulls(jsonb_build_object(
{_COMPOSITION}
    ))                                                          AS composition
"""

#: Tris proposés aux classements. Liste blanche obligatoire : la valeur vient de
#: l'URL et est interpolée dans un ORDER BY, qui n'accepte pas de paramètre lié.
#: PostgreSQL autorise le tri sur les alias de sortie, d'où ces noms de mesures.
_SORTS: dict[str, str] = {
    "volume": "avis_clients DESC NULLS LAST",
    "presse": "articles_presse DESC NULLS LAST",
    "negatifs": "part_negatifs DESC NULLS LAST",
    "note_asc": "note_moyenne ASC NULLS LAST",
    "note_desc": "note_moyenne DESC NULLS LAST",
    "score_asc": "score_moyen ASC NULLS LAST",
    "score_desc": "score_moyen DESC NULLS LAST",
}

#: Types d'alertes qui décrivent le MÉTIER (la satisfaction client) et non la
#: santé technique de la collecte. Le dashboard les sépare : mélangés, les 165
#: « scraper_zero » de la base enterrent le seul signal métier qu'elle contient.
BUSINESS_ALERT_TYPES = ("negative_spike",)


class StatsRepository:
    """Requêtes d'agrégats pour l'API et le dashboard temps réel.

    Chaque méthode reçoit un :class:`StatsFilter` et le traduit via son
    constructeur de WHERE. Aucun filtre n'est réécrit à la main ici : c'est ce
    qui garantit que période, pays, opérateur, filiale et source s'appliquent
    partout de la même façon.
    """

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------------
    # Options de la barre de filtres
    # ------------------------------------------------------------------------

    def filter_options(self) -> dict:
        """Valeurs proposées par la barre de filtres, avec leur volume.

        DÉLIBÉRÉMENT NON FILTRÉ : les listes couvrent tout le périmètre suivi,
        indépendamment de la sélection en cours. Une barre de filtres qui se
        restreint à sa propre sélection interdit d'élargir sans tout remettre à
        zéro.

        Le volume accompagne chaque entrée pour que le dashboard trie par
        importance et signale les entités sans aucun avis — il y en a : 96
        filiales sont déclarées, toutes n'ont pas encore d'identifiant de source
        vérifié.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT co.country_id AS id, co.iso2, co.name AS label, co.region,
                       COUNT(r.review_id) FILTER (WHERE src.kind = 'customer_review'
                                        AND src.comparable)
                                                                  AS avis_clients,
                       COUNT(r.review_id)                         AS total
                FROM dim_country co
                LEFT JOIN dim_subsidiary sub ON sub.country_id  = co.country_id
                LEFT JOIN reviews r          ON r.subsidiary_id = sub.subsidiary_id
                LEFT JOIN dim_source src     ON src.source_id   = r.source_id
                GROUP BY co.country_id, co.iso2, co.name, co.region
                ORDER BY co.name
                """
            )
            countries = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT op.operator_id AS id, op.name AS label, op.parent_group,
                       COUNT(DISTINCT sub.country_id)             AS nb_pays,
                       COUNT(r.review_id) FILTER (WHERE src.kind = 'customer_review'
                                        AND src.comparable)
                                                                  AS avis_clients,
                       COUNT(r.review_id)                         AS total
                FROM dim_operator op
                LEFT JOIN dim_subsidiary sub ON sub.operator_id  = op.operator_id
                LEFT JOIN reviews r          ON r.subsidiary_id  = sub.subsidiary_id
                LEFT JOIN dim_source src     ON src.source_id    = r.source_id
                GROUP BY op.operator_id, op.name, op.parent_group
                ORDER BY op.name
                """
            )
            operators = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT sub.subsidiary_id AS id, sub.name AS label,
                       op.operator_id, op.name AS operator,
                       co.country_id, co.iso2, co.name AS country,
                       COUNT(r.review_id) FILTER (WHERE src.kind = 'customer_review'
                                        AND src.comparable)
                                                                  AS avis_clients,
                       COUNT(r.review_id)                         AS total
                FROM dim_subsidiary sub
                JOIN dim_operator op ON op.operator_id = sub.operator_id
                JOIN dim_country  co ON co.country_id  = sub.country_id
                LEFT JOIN reviews r      ON r.subsidiary_id = sub.subsidiary_id
                LEFT JOIN dim_source src ON src.source_id   = r.source_id
                GROUP BY sub.subsidiary_id, sub.name, op.operator_id, op.name,
                         co.country_id, co.iso2, co.name
                ORDER BY sub.name
                """
            )
            subsidiaries = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT DISTINCT region AS label FROM dim_country "
                "WHERE region IS NOT NULL ORDER BY region"
            )
            regions = [r["label"] for r in cur.fetchall()]

            cur.execute(
                "SELECT code, name AS label, kind FROM dim_source ORDER BY kind, name"
            )
            sources = [dict(r) for r in cur.fetchall()]

        return {
            "countries": countries,
            "operators": operators,
            "subsidiaries": subsidiaries,
            "regions": regions,
            "sources": sources,
        }

    # ------------------------------------------------------------------------
    # Vue d'ensemble
    # ------------------------------------------------------------------------

    def overview(self, f: Optional[StatsFilter] = None) -> dict:
        """Indicateurs de tête du périmètre, et leur variation.

        Les mesures séparent avis clients et presse au lieu de renvoyer un total
        indistinct : c'est la correction du défaut central de la version
        précédente, qui noyait 1 087 avis négatifs de clients dans 13 536
        articles de presse neutres et annonçait 8,3 % de mécontentement là où le
        chiffre réel est 16,7 %.

        `previous` porte les mêmes mesures sur la fenêtre antérieure de durée
        égale, pour afficher une variation. Il vaut None quand aucune période
        n'est bornée : sur « tout l'historique », il n'y a pas d'avant.
        """
        f = f or StatsFilter()
        where, params = f.where()

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT {_MEASURES},
                       COUNT(DISTINCT v.subsidiary_id) AS nb_filiales,
                       COUNT(DISTINCT v.operator_id)   AS nb_operateurs,
                       COUNT(DISTINCT v.country_id)    AS nb_pays
                FROM v_reviews_enriched v
                {where}
                """,
                params,
            )
            current = dict(cur.fetchone())

            previous = None
            if f.has_time_bound():
                prev_where, prev_params = f.where(window=f.previous_window())
                cur.execute(
                    f"SELECT {_MEASURES} FROM v_reviews_enriched v {prev_where}",
                    prev_params,
                )
                previous = dict(cur.fetchone())

            # Fenêtre propre à cet indicateur : « collecté sur 24 h » doit rester
            # sur 24 h même quand l'utilisateur regarde douze mois. On conserve
            # en revanche les filtres d'organisation, sans quoi la tuile
            # parlerait d'un autre périmètre que le reste de l'écran.
            #
            # C'est aussi le seul indicateur assis sur `collected_at` (quand la
            # donnée est ENTRÉE) et non sur `created_at` (quand elle a été
            # PUBLIÉE) : il mesure l'activité du pipeline, pas celle du marché.
            scope_where, scope_params = f.where(include_time=False)
            cur.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                                        AND v.source_comparable)
                                AS avis_clients
                FROM v_reviews_enriched v
                {scope_where} AND v.collected_at > now() - interval '24 hours'
                """,
                scope_params,
            )
            last_24h = dict(cur.fetchone())

            # CE QUE LE PÉRIMÈTRE A LAISSÉ DE CÔTÉ, et pourquoi.
            #
            # Même discipline que `avis_hors_comparaison` : 20 107 avis
            # d'application quittent la satisfaction du service (migration 019).
            # Passés sous silence, ils rendraient inexplicable l'écart entre le
            # volume annoncé par la barre de filtres et celui des tuiles — et
            # feraient croire à une collecte en panne.
            #
            # La requête reprend le périmètre SANS le prédicat d'objet : c'est
            # exactement le volume de l'autre côté.
            autre_cote = replace(f, about=APP if f.about == OPERATOR else OPERATOR)
            other_where, other_params = autre_cote.where()
            cur.execute(
                f"""
                SELECT COUNT(*) FILTER (WHERE {_CLIENTS})       AS avis,
                       COUNT(*) FILTER (WHERE {_CLIENTS}
                                          AND v.sentiment = 'negative')
                                                                AS negatifs
                FROM v_reviews_enriched v
                {other_where}
                """,
                other_params,
            )
            ecarte = dict(cur.fetchone())

        return {
            "window": f.describe(),
            "current": current,
            "previous": previous,
            "last_24h": last_24h,
            # `about` dit ce qu'on regarde, `ecarte` ce qu'on ne regarde pas.
            # Vaut None sur `all` : rien n'est écarté, il n'y a pas d'autre côté.
            "ecarte": None if f.about == BOTH_SIDES else {"about": autre_cote.about, **ecarte},
        }

    # ------------------------------------------------------------------------
    # Courbes
    # ------------------------------------------------------------------------

    def trend(
        self,
        f: Optional[StatsFilter] = None,
        level: Optional[str] = None,
        granularity: Optional[str] = None,
        limit: int = 6,
    ) -> dict:
        """Séries temporelles du périmètre, éventuellement une par entité.

        Args:
            level: None pour une courbe unique ; sinon 'country', 'operator',
                'subsidiary', 'region' ou 'source' pour une courbe par entité —
                c'est ce qui rend possible la superposition de l'onglet Comparer.
            granularity: 'day', 'week' ou 'month'. Choisie automatiquement selon
                la durée si absente : un pas journalier sur douze mois produit
                365 points illisibles et masque la tendance qu'on cherche.
            limit: nombre maximal de séries, les plus volumineuses d'abord.
                Au-delà de cinq ou six courbes superposées, aucune n'est plus
                lisible : la limite est une contrainte de lecture avant d'être
                une contrainte de performance.

        Returns:
            {'granularity', 'level', 'window', 'series': [{key, label, points}]}
        """
        f = f or StatsFilter()
        start, end = f.resolved_window()
        gran = safe_granularity(granularity, (end - start).days)
        where, params = f.where()

        # `gran` est validé contre une liste blanche par safe_granularity :
        # date_trunc n'accepte pas de paramètre lié pour son premier argument,
        # l'interpolation est donc inévitable et la validation obligatoire.
        bucket = f"date_trunc('{gran}', COALESCE(v.created_at, v.collected_at))::date"

        if level is None:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    f"""
                    SELECT {bucket} AS bucket, {_MEASURES}
                    FROM v_reviews_enriched v
                    {where}
                    GROUP BY 1
                    ORDER BY 1
                    """,
                    params,
                )
                points = [dict(r) for r in cur.fetchall()]
            return {
                "granularity": gran,
                "level": None,
                "window": f.describe(),
                "series": [{"key": None, "label": "Périmètre", "points": points}],
            }

        _, lvl = resolve_level(level)
        extras = "".join(f", {col}" for col in lvl.extra)

        # `scoped` applique le filtre une seule fois ; `top_keys` retient les
        # entités les plus volumineuses. Sans cette restriction, une courbe par
        # filiale renverrait 96 séries — inexploitable à l'écran.
        #
        # `SELECT *` dans le CTE puis alias `v` : toutes les expressions
        # `v.colonne` du filtre et des mesures restent valides sans réécriture.
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                WITH scoped AS (
                    SELECT * FROM v_reviews_enriched v
                    {where}
                ),
                top_keys AS (
                    SELECT {lvl.key} AS k
                    FROM scoped v
                    WHERE {lvl.key} IS NOT NULL
                    GROUP BY 1
                    ORDER BY COUNT(*) DESC
                    LIMIT %s
                )
                SELECT {bucket} AS bucket,
                       {lvl.key} AS series_key,
                       {lvl.label} AS series_label{extras},
                       {_MEASURES}
                FROM scoped v
                JOIN top_keys tk ON tk.k = {lvl.key}
                GROUP BY 1, 2, 3{extras}
                ORDER BY 3, 1
                """,
                params + [limit],
            )
            rows = [dict(r) for r in cur.fetchall()]

        # Regroupement en séries côté Python : une seule requête, et le
        # dashboard reçoit directement la forme attendue par les bibliothèques
        # de tracé.
        series: dict[Any, dict] = {}
        for row in rows:
            key = row.pop("series_key")
            label = row.pop("series_label")
            bucket_value = row.pop("bucket")
            entry = series.setdefault(key, {"key": key, "label": label, "points": []})
            entry["points"].append({"bucket": bucket_value, **row})

        return {
            "granularity": gran,
            "level": level,
            "window": f.describe(),
            "series": list(series.values()),
        }

    # ------------------------------------------------------------------------
    # Classements
    # ------------------------------------------------------------------------

    def ranking(
        self,
        f: Optional[StatsFilter] = None,
        level: str = "subsidiary",
        sort: str = "negatifs",
        min_reviews: int = 0,
        limit: int = 200,
    ) -> dict:
        """Classement du périmètre à un niveau d'agrégation donné.

        Remplace by-country / by-operator / by-subsidiary, qui renvoyaient des
        cumuls depuis 2013 sans filtre possible : une filiale dégradée le mois
        dernier y était indiscernable d'une filiale dégradée en 2015.

        Args:
            min_reviews: exclut les entités sous ce nombre d'avis clients. Un
                taux calculé sur 3 avis n'est pas comparable à un taux calculé
                sur 400 ; le dashboard expose ce seuil plutôt que de présenter
                un classement dominé par le bruit.
        """
        f = f or StatsFilter()
        _, lvl = resolve_level(level)
        order = _SORTS.get(sort) or _SORTS["negatifs"]
        where, params = f.where()
        extras = "".join(f", {col}" for col in lvl.extra)

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT {lvl.key} AS key, {lvl.label} AS label{extras}, {_MEASURES}
                FROM v_reviews_enriched v
                {where}
                GROUP BY {lvl.key}, {lvl.label}{extras}
                HAVING {lvl.key} IS NOT NULL
                   AND COUNT(*) FILTER (WHERE v.source_kind = 'customer_review'
                                          AND v.source_comparable) >= %s
                -- Tri secondaire sur le libellé : sans lui, deux entités à
                -- mesure égale s'échangent de place d'un rafraîchissement à
                -- l'autre, ce qui donne l'impression d'un changement réel.
                ORDER BY {order}, label ASC
                LIMIT %s
                """,
                params + [min_reviews, limit],
            )
            rows = [dict(r) for r in cur.fetchall()]

        return {
            "level": level,
            "sort": sort,
            "min_reviews": min_reviews,
            "window": f.describe(),
            "rows": rows,
        }

    # ------------------------------------------------------------------------
    # Matrice opérateur × pays
    # ------------------------------------------------------------------------

    def matrix(self, f: Optional[StatsFilter] = None) -> dict:
        """Croisement opérateur × pays, pour lecture en carte de chaleur.

        C'est le seul format qui tienne 35 opérateurs sur 38 pays dans un écran :
        en barres, ce croisement demanderait 96 séries empilées illisibles.

        Les axes sont renvoyés à part, ordonnés par volume décroissant, pour que
        le dashboard puisse dessiner une grille complète — cellules vides
        comprises. Une case vide est une information : cet opérateur n'est pas
        suivi dans ce pays.
        """
        f = f or StatsFilter()
        where, params = f.where()

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT v.operator_id, v.operator, v.country_id, v.country, v.iso2,
                       v.subsidiary_id, v.subsidiary, {_MEASURES}
                FROM v_reviews_enriched v
                {where}
                GROUP BY v.operator_id, v.operator, v.country_id, v.country,
                         v.iso2, v.subsidiary_id, v.subsidiary
                HAVING v.operator_id IS NOT NULL AND v.country_id IS NOT NULL
                ORDER BY total DESC
                """,
                params,
            )
            cells = [dict(r) for r in cur.fetchall()]

        # Axes déduits des cellules, sans requête supplémentaire : on ne veut
        # précisément que les entités présentes dans le périmètre filtré.
        def axis(id_key: str, label_key: str) -> list[dict]:
            totals: dict[Any, dict] = {}
            for cell in cells:
                entry = totals.setdefault(
                    cell[id_key],
                    {
                        "id": cell[id_key],
                        "label": cell[label_key],
                        "total": 0,
                        "avis_clients": 0,
                    },
                )
                entry["total"] += cell["total"]
                entry["avis_clients"] += cell["avis_clients"]
            return sorted(totals.values(), key=lambda e: -e["total"])

        return {
            "window": f.describe(),
            "operators": axis("operator_id", "operator"),
            "countries": axis("country_id", "country"),
            "cells": cells,
        }

    # ------------------------------------------------------------------------
    # Variations
    # ------------------------------------------------------------------------

    def movers(
        self,
        f: Optional[StatsFilter] = None,
        level: str = "subsidiary",
        limit: int = 5,
        min_reviews: int = 20,
    ) -> dict:
        """Entités qui se sont le plus dégradées ou améliorées sur la période.

        C'est la réponse à « qu'est-ce qui a changé ? », question qu'un cumul
        depuis 2013 ne peut pas traiter.

        La variation est exprimée en POINTS de part de négatifs, pas en
        pourcentage de variation : passer de 10 % à 15 % se dit « +5 pts », une
        formulation qui ne dépend pas du point de départ, là où « +50 % » serait
        à la fois exact et trompeur.

        Args:
            min_reviews: seuil d'avis clients exigé sur LES DEUX fenêtres. Sans
                lui, le classement est monopolisé par des entités passant de 1 à
                2 avis, dont l'écart de taux atteint mécaniquement 100 pts.
        """
        f = f or StatsFilter()
        if not f.has_time_bound():
            # Aucune période bornée : la fenêtre antérieure démarre avant le
            # plancher de données et serait vide. Mieux vaut le dire que
            # renvoyer des variations calculées contre rien.
            return {
                "level": level,
                "available": False,
                "reason": "Bornez une période pour obtenir des variations.",
                "window": f.describe(),
                "degraded": [],
                "improved": [],
            }

        current = self._grouped(f, level, f.resolved_window())
        previous = self._grouped(f, level, f.previous_window())
        prev_by_key = {row["key"]: row for row in previous}

        moves: list[dict] = []
        for row in current:
            before = prev_by_key.get(row["key"])
            if before is None:
                continue  # entité absente avant : aucune variation calculable
            if row["avis_clients"] < min_reviews or before["avis_clients"] < min_reviews:
                continue
            if row["part_negatifs"] is None or before["part_negatifs"] is None:
                continue
            delta = float(row["part_negatifs"]) - float(before["part_negatifs"])
            moves.append(
                {
                    **row,
                    "part_negatifs_avant": before["part_negatifs"],
                    "note_moyenne_avant": before["note_moyenne"],
                    "avis_clients_avant": before["avis_clients"],
                    "delta_negatifs": round(delta, 1),
                }
            )

        moves.sort(key=lambda m: m["delta_negatifs"], reverse=True)
        degraded = [m for m in moves if m["delta_negatifs"] > 0][:limit]
        improved = [m for m in reversed(moves) if m["delta_negatifs"] < 0][:limit]

        prev_start, prev_end = f.previous_window()
        return {
            "level": level,
            "available": True,
            "min_reviews": min_reviews,
            "window": f.describe(),
            "previous_window": {
                "from": prev_start.isoformat(),
                "to": prev_end.isoformat(),
            },
            "degraded": degraded,
            "improved": improved,
        }

    def _grouped(self, f: StatsFilter, level: str, window: tuple) -> list[dict]:
        """Mesures par entité sur une fenêtre donnée (brique de `movers`)."""
        _, lvl = resolve_level(level)
        where, params = f.where(window=window)
        extras = "".join(f", {col}" for col in lvl.extra)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT {lvl.key} AS key, {lvl.label} AS label{extras}, {_MEASURES}
                FROM v_reviews_enriched v
                {where}
                GROUP BY {lvl.key}, {lvl.label}{extras}
                HAVING {lvl.key} IS NOT NULL
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------------
    # Motifs d'insatisfaction
    # ------------------------------------------------------------------------

    #: Les deux façons de répondre à « pourquoi ? », et ce qui les distingue.
    #:
    #: `terms` agrège les mots du lexique qui se sont déclenchés (migration 004).
    #: `aspects` agrège les aspects métier reconnus par l'analyse sémantique
    #: (migration 005). Les deux vues sont volontairement jumelles — mêmes
    #: colonnes, mêmes axes de filtre — de sorte qu'une seule requête les serve.
    _DIMENSIONS: dict[str, tuple[str, str, str]] = {
        # nom -> (vue, colonne du libellé, colonne signalant l'absence d'analyse)
        "terms": ("v_review_terms", "term", "v.lexicon_version IS NULL"),
        "aspects": ("v_review_aspects", "aspect", "v.aspect_version IS NULL"),
    }

    def themes(
        self,
        f: Optional[StatsFilter] = None,
        polarity: str = "negative",
        limit: int = 25,
        dimension: str = "terms",
    ) -> dict:
        """Motifs les plus fréquents sur le périmètre — le « pourquoi ».

        Args:
            dimension: `terms` pour les mots du lexique (migration 004),
                `aspects` pour les aspects métier de l'analyse sémantique
                (migration 005).

                LA DIFFÉRENCE EST DE NATURE, pas de finesse. Le lexique ne peut
                remonter que des MOTS PRÉSENTS dans le texte : mesuré sur 90
                jours, il classe en tête des motifs d'insatisfaction « can't »,
                « bad », « doesn't », « its », « without » — statistiquement
                corrélés au mécontentement, mais ne nommant aucun problème
                traitable. Les aspects, eux, sont choisis dans une taxonomie
                fermée par un modèle qui lit la phrase : « le réseau tombe tous
                les soirs » produit `coupures_pannes` sans contenir un seul mot
                du lexique.

        `nb_filiales` distingue un motif SYSTÉMIQUE (présent partout, donc
        structurel) d'un motif LOCAL (concentré, donc actionnable sur place) :
        deux motifs de même volume n'appellent pas la même décision.

        Restreint par défaut aux avis clients : le vocabulaire de la presse est
        journalistique, ce n'est pas une plainte de client. Un `source_kind`
        explicite dans le filtre est respecté.
        """
        f = f or StatsFilter()
        view, label_col, pending_expr = self._resolve_dimension(dimension)
        kind = f.source_kind or CUSTOMER
        # TERMS et ASPECTS décrivent deux vues aliasées `t` avec les mêmes noms
        # de colonnes : le même constructeur de filtre s'applique aux deux.
        where, params = f.where(cols=TERMS, source_kind=kind)

        # « autre » est ÉCARTÉ du classement, et compté à part.
        #
        # Mesuré sur le premier lot réel : la moitié des avis n'exprime aucun
        # motif identifiable (« good », « excellent », « perfect one »). Les
        # classer « autre » est exact — c'est le repli prévu par la taxonomie —
        # mais laisser ce sac occuper la première barre du graphique
        # reproduirait précisément le défaut qu'on corrige : un motif en tête de
        # classement dont on ne peut rien faire.
        #
        # Il n'est pas perdu pour autant : `base.sans_motif` le restitue, et
        # « 40 % des avis positifs ne citent rien de précis » est en soi une
        # information sur la qualité du corpus.
        exclusion, exclusion_params = "", []
        if dimension == "aspects":
            exclusion = f" AND t.{label_col} <> %s"
            exclusion_params = [OTHER]

            # Les aspects qui contredisent le côté demandé sortent aussi.
            #
            # `about` garde l'AVIS mixte dans le périmètre du service — il y a
            # bien une plainte de service dedans. Mais son aspect applicatif n'a
            # rien à faire dans un classement des motifs de service : mesuré
            # après la migration 019, « Bugs de l'application » y arrivait
            # quatrième avec 1 030 avis, et l'Agent 1 le recopiait dans un
            # briefing annonçant une dégradation du service.
            scope_clause, scope_params = f.aspect_scope_clause()
            exclusion += scope_clause
            exclusion_params += scope_params

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT t.{label_col}                   AS term,
                       COUNT(*)                        AS avis,
                       COUNT(DISTINCT t.subsidiary_id) AS nb_filiales,
                       COUNT(DISTINCT t.country_id)    AS nb_pays,
                       ROUND(AVG(t.sentiment_score)::numeric, 3) AS score_moyen
                FROM {view} t
                {where} AND t.polarity = %s{exclusion}
                GROUP BY t.{label_col}
                ORDER BY avis DESC, t.{label_col} ASC
                LIMIT %s
                """,
                params + [polarity] + exclusion_params + [limit],
            )
            terms = [dict(r) for r in cur.fetchall()]

            # Libellé lisible d'un aspect, résolu ICI et non dans le dashboard.
            # La taxonomie vit en Python ; la recopier en TypeScript créerait
            # deux listes à tenir, qui divergeraient au premier ajout d'aspect.
            if dimension == "aspects":
                for row in terms:
                    row["label"] = aspect_label(row["term"])

            # Dénominateur du périmètre. Sans lui, « 120 avis mentionnent
            # coupure » ne se compare pas d'une filiale à l'autre.
            # `non_analyses` signale honnêtement les avis pas encore traités par
            # la dimension demandée : le total des motifs est alors incomplet, et
            # le dashboard doit pouvoir le dire au lieu de laisser croire à zéro.
            base_where, base_params = f.where(source_kind=kind)
            cur.execute(
                f"""
                SELECT COUNT(*) AS avis_analyses,
                       COUNT(*) FILTER (WHERE {pending_expr}) AS non_analyses
                FROM v_reviews_enriched v
                {base_where}
                """,
                base_params,
            )
            base = dict(cur.fetchone())

            # Avis analysés dont le modèle n'a tiré AUCUN motif nommable.
            # Restitué plutôt que masqué : sans ce chiffre, le total des motifs
            # ne se recoupe pas avec le nombre d'avis, et rien ne l'explique.
            if dimension == "aspects":
                column = "neg_aspects" if polarity == "negative" else "pos_aspects"
                cur.execute(
                    f"""
                    SELECT COUNT(*) AS sans_motif
                    FROM v_reviews_enriched v
                    {base_where}
                      AND v.aspect_version IS NOT NULL
                      AND %s = ANY(v.{column})
                    """,
                    base_params + [OTHER],
                )
                base["sans_motif"] = dict(cur.fetchone())["sans_motif"]

        return {
            "polarity": polarity,
            "dimension": dimension,
            "source_kind": kind,
            "window": f.describe(),
            "base": base,
            "terms": terms,
        }

    def _resolve_dimension(self, name: str) -> tuple[str, str, str]:
        """Résout un nom de dimension venu de l'URL, ou lève une erreur explicite.

        Liste blanche obligatoire : le nom sert à composer un nom de VUE et de
        COLONNE, qui ne peuvent pas être des paramètres liés.
        """
        found = self._DIMENSIONS.get(name)
        if found is None:
            raise ValueError(
                f"Dimension « {name} » inconnue. Valeurs acceptées : "
                + ", ".join(sorted(self._DIMENSIONS))
            )
        return found

    def semantic_coverage(self, f: Optional[StatsFilter] = None) -> dict:
        """Part du périmètre effectivement passée par l'analyse sémantique.

        Nécessaire pour lire honnêtement les taux : tant que la couverture est
        partielle, la part de négatifs mélange deux classifieurs — le modèle là
        où il est passé, le lexique ailleurs. Le dashboard doit l'annoncer, pas
        le taire.
        """
        f = f or StatsFilter()
        kind = f.source_kind or CUSTOMER
        where, params = f.where(source_kind=kind)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE v.sentiment_source = 'llm') AS analyses
                FROM v_reviews_enriched v
                {where}
                """,
                params,
            )
            row = dict(cur.fetchone())
        total = row["total"] or 0
        row["part"] = round(100.0 * row["analyses"] / total, 1) if total else None
        return row

    def verbatims(
        self,
        f: Optional[StatsFilter] = None,
        term: Optional[str] = None,
        polarity: str = "negative",
        limit: int = 20,
    ) -> dict:
        """Avis d'exemple du périmètre, éventuellement filtrés sur un terme.

        Le chiffre dit combien, le verbatim dit quoi. Un motif sans exemple
        n'est pas défendable devant un métier : « 120 avis mentionnent coupure »
        ne devient exploitable qu'accompagné de trois avis qu'on peut lire.

        Tri du plus négatif au moins négatif (ou l'inverse pour la polarité
        positive) : ce sont les avis extrêmes qui portent l'information, pas les
        avis médians.

        CITER N'EST PAS COMPTER — d'où la double passe ci-dessous. Un avis qui
        nomme un grief de service ET un bug d'application entre légitimement
        dans le TAUX du service, mais fait un très mauvais EXEMPLE : mesuré sur
        les trois pics du 16 août, les avis cités en preuve étaient tous des
        avis mixtes au texte dominé par l'application (« App is typically
        good... however the app is currently not starting up at all » sous un
        « pic de mécontentement » censé porter sur le service).

        On demande donc d'abord les avis qui ne parlent QUE du côté regardé, et
        on ne complète avec les avis mixtes que si les premiers ne suffisent
        pas — un pic sans aucune citation serait pire qu'une citation imparfaite.
        `about_strict` dit lequel des deux cas s'est produit, plutôt que de
        laisser croire à une pureté qui n'est pas toujours atteinte.
        """
        f = f or StatsFilter()
        kind = f.source_kind or CUSTOMER

        rows = self._verbatims_rows(replace(f, about_strict=True), kind, term, polarity, limit)
        strict = len(rows) >= limit
        if not strict and f.about != BOTH_SIDES:
            vus = {r["review_id"] for r in rows}
            complement = self._verbatims_rows(
                replace(f, about_strict=False), kind, term, polarity, limit
            )
            rows += [r for r in complement if r["review_id"] not in vus]
            rows = rows[:limit]

        return {
            "term": term,
            "polarity": polarity,
            "window": f.describe(),
            "about_strict": strict,
            "reviews": rows,
        }

    def _verbatims_rows(
        self,
        f: StatsFilter,
        kind: str,
        term: Optional[str],
        polarity: str,
        limit: int,
    ) -> list[dict]:
        """Une passe de `verbatims` — brique des deux appels ci-dessus."""
        where, params = f.where(source_kind=kind)

        clause, extra_params = "", []
        if term:
            column = "v.neg_terms" if polarity == "negative" else "v.pos_terms"
            # `%s = ANY(colonne)` exploite l'index GIN de la migration 004 :
            # pas de scan de la table à chaque clic sur un motif.
            clause = f" AND %s = ANY({column})"
            extra_params = [term]

        order = "ASC" if polarity == "negative" else "DESC"

        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT v.review_id, v.title,
                       -- Tronqué en SQL : certains articles de presse font
                       -- plusieurs milliers de caractères, inutiles à
                       -- transporter pour un extrait affiché sur trois lignes.
                       LEFT(v.text, 600) AS text,
                       LENGTH(v.text) > 600 AS text_tronque,
                       v.rating, v.sentiment, v.sentiment_score,
                       v.neg_terms, v.pos_terms,
                       v.subsidiary, v.operator, v.country, v.iso2, v.source,
                       COALESCE(v.created_at, v.collected_at) AS occurred_at
                FROM v_reviews_enriched v
                {where}{clause}
                ORDER BY v.sentiment_score {order} NULLS LAST,
                         COALESCE(v.created_at, v.collected_at) DESC
                LIMIT %s
                """,
                params + extra_params + [limit],
            )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------------
    # Fil d'actualité
    # ------------------------------------------------------------------------

    def feed(
        self,
        f: Optional[StatsFilter] = None,
        limit: int = 25,
        sentiment: Optional[str] = None,
    ) -> dict:
        """Derniers contenus du périmètre : avis clients et presse.

        CHRONOLOGIQUE, ET NON TRIÉ PAR SCORE. C'est ce qui distingue ce fil de
        `verbatims` : celui-ci répond à « que dit-on de nous en ce moment »,
        l'autre à « quels sont les avis les plus extrêmes sur ce motif ». Trier
        un fil d'actualité par intensité en ferait un florilège de réclamations,
        où l'on ne saurait plus ce qui est récent.

        LES DEUX FLUX SONT RENDUS SÉPARÉMENT, jamais fusionnés. La presse est
        environ deux fois plus volumineuse que les avis clients : entrelacés par
        date, les avis disparaîtraient sous les articles, et l'écran cesserait
        de montrer la voix du client.

        Le filtre `source_kind` du périmètre est respecté : demander la presse
        seule laisse la liste d'avis vide plutôt que de la remplir en ignorant
        ce que l'utilisateur a demandé.
        """
        f = f or StatsFilter()
        kinds = (f.source_kind,) if f.source_kind else (CUSTOMER, PRESS)

        out: dict[str, list[dict]] = {CUSTOMER: [], PRESS: []}
        for kind in kinds:
            where, params = f.where(source_kind=kind)

            clause, extra = "", []
            if sentiment:
                clause = " AND v.sentiment = %s"
                extra = [sentiment]

            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(
                    f"""
                    SELECT v.review_id, v.title,
                           -- Tronqué en SQL : un article de presse fait
                           -- couramment plusieurs milliers de caractères, sans
                           -- objet pour un extrait affiché sur quatre lignes.
                           LEFT(v.text, 400) AS text,
                           LENGTH(v.text) > 400 AS text_tronque,
                           v.rating, v.sentiment, v.sentiment_score,
                           v.neg_aspects, v.pos_aspects,
                           v.subsidiary, v.operator, v.parent_group,
                           v.country, v.iso2,
                           v.source, v.source_code, v.source_kind,
                           COALESCE(v.created_at, v.collected_at) AS occurred_at,
                           -- Distingue « publié le » de « reçu le ». Une source
                           -- qui ne date pas ses contenus se voit attribuer sa
                           -- date de collecte ; le dire évite de présenter une
                           -- date de collecte comme une date de publication.
                           v.created_at IS NULL AS date_estimee
                    FROM v_reviews_enriched v
                    {where}{clause}
                    ORDER BY COALESCE(v.created_at, v.collected_at) DESC
                    LIMIT %s
                    """,
                    params + extra + [limit],
                )
                out[kind] = [dict(r) for r in cur.fetchall()]

        return {
            "window": f.describe(),
            "sentiment": sentiment,
            "avis": out[CUSTOMER],
            "presse": out[PRESS],
        }

    # ------------------------------------------------------------------------
    # Contexte d'un point de courbe
    # ------------------------------------------------------------------------

    def point_context(
        self,
        f: Optional[StatsFilter] = None,
        level: Optional[str] = None,
        entities: tuple[str, ...] = (),
        granularity: Optional[str] = None,
    ) -> dict:
        """De quoi expliquer, au survol, ce qu'un point de courbe montre.

        POURQUOI CE N'EST PAS UNE SYNTHÈSE LLM. Un survol doit répondre
        instantanément et ne rien coûter : ici tout est calculé en base. Le
        modèle n'intervient pas, donc il n'invente rien — et le quota reste
        disponible pour les synthèses demandées explicitement.

        L'ORDRE DES ÉLÉMENTS N'EST PAS ANODIN. Mesuré sur 90 jours : deux points
        sur trois (353 sur 523) reposent sur MOINS DE CINQ AVIS. Sur trois avis,
        une part de négatifs ne peut valoir que 0, 33, 67 ou 100 % — l'inflexion
        la plus spectaculaire est alors un artefact d'échantillon, pas un
        événement. C'est donc le volume qui prime dans la phrase affichée :
        proposer une cause de presse devant un pic construit sur trois avis
        donnerait un sens à du bruit, ce qui est pire que de se taire.

        La presse ne couvre que 8,6 % des couples (filiale, semaine) : elle
        complète, elle ne porte pas la fonctionnalité.
        """
        f = f or StatsFilter()
        name, lvl = resolve_level(level)
        start, end = f.resolved_window()
        gran = safe_granularity(granularity, (end - start).days)

        # `gran` vient d'une liste blanche : date_trunc n'accepte pas de
        # paramètre lié pour son premier argument.
        bucket = f"date_trunc('{gran}', COALESCE(v.created_at, v.collected_at))::date"

        # Le filtre d'entités s'exprime en texte quel que soit le type de la
        # clé (entier pour une filiale, texte pour une région).
        clause, extra = "", []
        if entities:
            clause = f" AND {lvl.key}::text = ANY(%s)"
            extra = [list(entities)]

        where_client, params_client = f.where(source_kind=CUSTOMER)
        where_presse, params_presse = f.where(source_kind=PRESS)

        with self.db.cursor(dict_rows=True) as cur:
            # 1. Volume par point : la mesure qui décide si le reste vaut
            #    d'être dit.
            cur.execute(
                f"""
                SELECT {lvl.key}::text AS cle, {lvl.label} AS label,
                       {bucket} AS bucket, COUNT(*) AS avis
                FROM v_reviews_enriched v
                {where_client}{clause}
                GROUP BY 1, 2, 3
                """,
                params_client + extra,
            )
            volumes = [dict(r) for r in cur.fetchall()]

            # 2. Motif négatif dominant par point.
            cur.execute(
                f"""
                SELECT cle, bucket, motif, avis FROM (
                    SELECT {lvl.key}::text AS cle,
                           {bucket} AS bucket,
                           unnest(v.neg_aspects) AS motif,
                           COUNT(*) AS avis,
                           ROW_NUMBER() OVER (
                               PARTITION BY {lvl.key}::text, {bucket}
                               ORDER BY COUNT(*) DESC
                           ) AS rang
                    FROM v_reviews_enriched v
                    {where_client}{clause}
                      AND v.neg_aspects IS NOT NULL
                    GROUP BY 1, 2, 3
                ) x WHERE rang = 1
                """,
                params_client + extra,
            )
            motifs = {(r["cle"], r["bucket"]): dict(r) for r in cur.fetchall()}

            # 3. Presse du même point. Deux titres suffisent : la carte de
            #    survol doit rester lisible d'un coup d'œil.
            cur.execute(
                f"""
                SELECT cle, bucket, title, source FROM (
                    SELECT {lvl.key}::text AS cle,
                           {bucket} AS bucket,
                           v.title, v.source,
                           ROW_NUMBER() OVER (
                               PARTITION BY {lvl.key}::text, {bucket}
                               ORDER BY COALESCE(v.created_at, v.collected_at) DESC
                           ) AS rang
                    FROM v_reviews_enriched v
                    {where_presse}{clause}
                ) x WHERE rang <= 2
                """,
                params_presse + extra,
            )
            presse: dict[tuple, list] = {}
            for r in cur.fetchall():
                presse.setdefault((r["cle"], r["bucket"]), []).append(
                    {"title": r["title"], "source": r["source"]}
                )

        # Assemblage par série, dans l'ordre chronologique : le motif de la
        # période PRÉCÉDENTE se lit alors sans requête supplémentaire, et c'est
        # lui qui transforme « 8 avis parlent de bugs » en « 8 contre 2 ».
        series: dict[str, dict] = {}
        for v in sorted(volumes, key=lambda r: (r["cle"], r["bucket"])):
            cle = v["cle"]
            serie = series.setdefault(cle, {"key": cle, "label": v["label"], "points": []})
            motif = motifs.get((cle, v["bucket"]))
            point = {
                "bucket": v["bucket"].isoformat(),
                "avis_clients": v["avis"],
                "motif": None,
                "presse": presse.get((cle, v["bucket"]), []),
            }
            if motif:
                point["motif"] = {
                    "term": motif["motif"],
                    "label": aspect_label(motif["motif"]),
                    "avis": motif["avis"],
                }
            serie["points"].append(point)

        for serie in series.values():
            precedent: dict[str, int] = {}
            for point in serie["points"]:
                m = point["motif"]
                if m:
                    m["avis_avant"] = precedent.get(m["term"])
                    precedent = {m["term"]: m["avis"]}
                else:
                    precedent = {}

        return {
            "level": name,
            "granularity": gran,
            "window": f.describe(),
            #: En dessous, une part se calcule sur si peu d'avis qu'elle ne peut
            #: prendre que quelques valeurs. Le seuil est celui déjà employé par
            #: `reliableShare` côté interface, pour que les deux écrans ne
            #: qualifient pas différemment le même point.
            "seuil_fragile": 5,
            "series": list(series.values()),
        }

    # ------------------------------------------------------------------------
    # Santé de la collecte
    # ------------------------------------------------------------------------

    def pipeline_health(self, runs_limit: int = 10) -> dict:
        """Fraîcheur par source et derniers runs — la preuve que la chaîne tourne.

        Volontairement NON filtré par période : la question posée ici est « la
        collecte fonctionne-t-elle en ce moment ? », pas « qu'a-t-elle collecté
        en mars ? ».

        Cet écran rend lisible ce que la base contenait déjà sans que personne
        ne puisse le consulter : 165 alertes `scraper_zero` et 35
        `high_duplicates` signalent des collecteurs en panne silencieuse.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT src.code, src.name, src.kind, src.has_rating,
                       MAX(r.collected_at)                        AS derniere_collecte,
                       MAX(COALESCE(r.created_at, r.collected_at)) AS dernier_contenu,
                       COUNT(r.review_id)                         AS total,
                       COUNT(r.review_id) FILTER (
                           WHERE r.collected_at > now() - interval '24 hours') AS h24,
                       COUNT(r.review_id) FILTER (
                           WHERE r.collected_at > now() - interval '7 days')   AS j7,
                       -- Âge de la donnée la plus récente, en heures. C'est
                       -- l'indicateur qui révèle une source morte : un total
                       -- élevé n'empêche pas d'avoir cessé de collecter hier.
                       ROUND((EXTRACT(EPOCH FROM (now() - MAX(r.collected_at))) / 3600)::numeric, 1)
                                                                  AS heures_depuis_collecte
                FROM dim_source src
                LEFT JOIN reviews r ON r.source_id = src.source_id
                GROUP BY src.code, src.name, src.kind, src.has_rating
                ORDER BY src.kind, src.name
                """
            )
            sources = [dict(r) for r in cur.fetchall()]

            # État d'ACTIVATION, lu dans la configuration et non dans la base.
            #
            # Sans lui, une source volontairement désactivée est indiscernable
            # d'une source en panne : les deux affichent « jamais collecté ». Le
            # dashboard signalerait alors un problème là où il y a eu une
            # décision — et Trustpilot, désactivé faute de fiches africaines,
            # passerait indéfiniment pour un collecteur cassé.
            #
            # Les noms de collecteurs diffèrent des codes de source stockés en
            # base (« appstore » contre « app_store ») : la correspondance est
            # donc explicite plutôt que déduite.
            actifs = set(get_settings().get_enabled_scrapers())
            collecteur_de = {
                "app_store": "appstore",
                "google_play": "playstore",
                "google_maps": "googlemaps",
                "rss_feed": "rss_feed",
                "trustpilot": "trustpilot",
            }
            for source in sources:
                nom = collecteur_de.get(source["code"], source["code"])
                source["active"] = nom in actifs

            cur.execute(
                """
                SELECT pr.run_id, pr.started_at, pr.ended_at, pr.status,
                       pr.total_reviews, pr.total_duplicates, pr.total_errors,
                       pr.duration_seconds,
                       -- Taux de doublons : une collecte qui rejette la quasi-
                       -- totalité de ce qu'elle télécharge fonctionne, mais
                       -- gaspille — et ce gaspillage plafonne la fréquence de
                       -- collecte.
                       --
                       -- NE PAS écrire le caractère pourcent dans un commentaire
                       -- SQL passé à psycopg2 AVEC des paramètres : il est lu
                       -- comme un début de marqueur de substitution et lève
                       -- « IndexError: tuple index out of range ». Le doubler
                       -- (%%) fonctionne mais se perd à la première relecture.
                       ROUND((100.0 * pr.total_duplicates
                              / NULLIF(pr.total_reviews + pr.total_duplicates, 0))::numeric, 1)
                                                             AS taux_doublons,
                       COUNT(rm.metric_id) FILTER (WHERE rm.status = 'failed')
                                                             AS collecteurs_en_echec,
                       COUNT(rm.metric_id)                   AS collecteurs
                FROM pipeline_runs pr
                LEFT JOIN run_metrics rm ON rm.run_id = pr.run_id
                GROUP BY pr.run_id, pr.started_at, pr.ended_at, pr.status,
                         pr.total_reviews, pr.total_duplicates, pr.total_errors,
                         pr.duration_seconds
                ORDER BY pr.started_at DESC
                LIMIT %s
                """,
                (runs_limit,),
            )
            runs = [dict(r) for r in cur.fetchall()]

            cur.execute(
                """
                SELECT rm.scraper_name,
                       SUM(rm.inserted_count)  AS inseres,
                       SUM(rm.duplicate_count) AS doublons,
                       SUM(rm.error_count)     AS erreurs,
                       ROUND(AVG(rm.duration_seconds)::numeric, 1) AS duree_moyenne,
                       COUNT(*) FILTER (WHERE rm.status = 'failed') AS echecs,
                       COUNT(*)                AS executions,
                       MAX(rm.recorded_at)     AS derniere_execution
                FROM run_metrics rm
                WHERE rm.recorded_at > now() - interval '7 days'
                GROUP BY rm.scraper_name
                ORDER BY rm.scraper_name
                """
            )
            collectors = [dict(r) for r in cur.fetchall()]

        return {"sources": sources, "runs": runs, "collectors": collectors}

    # ------------------------------------------------------------------------
    # Endpoints historiques — conservés pour compatibilité
    # ------------------------------------------------------------------------
    # Les méthodes ci-dessous alimentaient le dashboard avant l'introduction du
    # filtre. Elles renvoient des cumuls SANS période ni filtre. Ne rien
    # construire de nouveau dessus : utiliser `ranking()` et `trend()`.

    def sentiment_trend(
        self, days: int = 30, company: Optional[str] = None
    ) -> list[dict]:
        """Tendance quotidienne (historique). Préférer `trend()`."""
        clause = "WHERE day >= current_date - %s"
        params: list[Any] = [days]
        if company:
            clause += " AND company = %s"
            params.append(company)
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                f"""
                SELECT day, SUM(total) AS total, SUM(positive) AS positive,
                       SUM(neutral) AS neutral, SUM(negative) AS negative,
                       ROUND(AVG(avg_rating)::numeric, 2) AS avg_rating
                FROM sentiment_daily
                {clause}
                GROUP BY day ORDER BY day
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def by_company(self) -> list[dict]:
        """Répartition par nom libre d'entreprise (historique)."""
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT company,
                       COUNT(*)                                     AS total,
                       COUNT(*) FILTER (WHERE sentiment='negative') AS negative,
                       ROUND(AVG(rating)::numeric, 2)               AS avg_rating
                FROM reviews GROUP BY company ORDER BY total DESC
                """
            )
            return [dict(r) for r in cur.fetchall()]

    def by_country(self) -> list[dict]:
        """Cumul par pays, sans filtre (historique). Préférer `ranking()`."""
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute("SELECT * FROM v_stats_by_country ORDER BY avis_clients DESC")
            return [dict(r) for r in cur.fetchall()]

    def by_operator(self) -> list[dict]:
        """Cumul par opérateur, sans filtre (historique). Préférer `ranking()`."""
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute("SELECT * FROM v_stats_by_operator ORDER BY avis_clients DESC")
            return [dict(r) for r in cur.fetchall()]

    def by_subsidiary(self) -> list[dict]:
        """Cumul par filiale, sans filtre (historique). Préférer `ranking()`."""
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute("SELECT * FROM v_stats_by_subsidiary ORDER BY avis_clients DESC")
            return [dict(r) for r in cur.fetchall()]
