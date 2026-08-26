"""
Articles de presse servant de PREUVE à une explication externe.

LE PROBLÈME QUE CE MODULE RÉSOUT
    `llm/insights.py` sait dire QUOI a changé — la part de négatifs, les motifs,
    les volumes. Il ne sait pas dire POURQUOI, parce que la cause d'un
    basculement d'opinion est presque toujours extérieure au corpus d'avis : une
    hausse tarifaire, une panne nationale, une décision du régulateur.

    Demander cette cause à un modèle sans lui fournir de matière produit une
    réponse plausible et invérifiable — exactement ce que l'architecture de cette
    couche s'interdit depuis le début. Ce module fournit la matière : des
    articles DATÉS, rattachés à un périmètre, que le modèle peut citer ou taire.

POURQUOI LE PÉRIMÈTRE S'ÉLARGIT AU LIEU D'ÊTRE FIXE
    Mesuré sur le corpus réel (10 août 2026) : MTN Nigeria porte 823 avis
    clients sur 90 jours et SEULEMENT 2 articles de presse sur la même fenêtre.
    Chercher la cause d'un pic strictement dans la presse nommant la filiale
    reviendrait donc à ne rien trouver presque à chaque fois.

    Or l'événement qui fait chuter la satisfaction d'une filiale est rarement
    rapporté à la maille de cette filiale. Une décision de l'ARCEP togolaise
    frappe tous les opérateurs du Togo ; une panne de câble sous-marin frappe
    une région. `press_relevance` fait déjà cette observation pour le
    vocabulaire — « Internet mobile au Bénin : colère après la fin des forfaits
    illimités » ne nomme aucun opérateur et décrit pourtant l'événement.

    On élargit donc filiale → opérateur → pays, en s'arrêtant au premier
    périmètre qui donne de la matière. Le périmètre effectivement retenu est
    RENVOYÉ À L'APPELANT, qui le transmet au modèle : un article national ne doit
    jamais être présenté comme parlant de la filiale.

POURQUOI UNE AMORCE AVANT LA FENÊTRE
    Une cause précède son effet. Un client mécontent d'une hausse tarifaire
    annoncée le 3 écrit son avis le 10. Ne regarder la presse que sur la fenêtre
    d'analyse écarterait mécaniquement l'article qui l'explique. L'appelant passe
    donc une fenêtre déjà élargie vers l'amont.

COMMENT LES ARTICLES SONT CHOISIS
    Trois filtres, puis un tri.

      1. PÉRIMÈTRE — la filiale, élargi au pays si elle n'a pas de presse
         propre. Le périmètre retenu est renvoyé à l'appelant, qui doit le
         dire : « un article national » et « un article sur cette filiale »
         n'ont pas la même valeur.
      2. FENÊTRE — celle de l'analyse, élargie vers l'amont par l'appelant.
      3. TONALITÉ — le POSITIF est écarté. Une amélioration annoncée
         n'explique pas un mécontentement, et la proposer comme cause
         décrédibilise tout le bloc. Le neutre est conservé : une décision de
         régulateur ou une hausse tarifaire est rédigée sans affect, et c'est
         précisément ce qu'on cherche.

    Le tri place le NÉGATIF devant, puis le plus récent. Entre une panne et un
    communiqué anodin du même jour, la panne est le meilleur candidat.

CE QUE CE MODULE NE FAIT PAS
    Il ne juge pas qu'un article EXPLIQUE le pic. Il rassemble des candidats
    datés sur le bon périmètre ; le rapprochement reste au modèle, sous une
    contrainte de citation. Rien ici ne prétend établir une causalité.
"""

import logging
from datetime import date
from typing import Any, Optional

from reviews.domain.press_relevance import est_pertinent
from reviews.storage.db import Database
from reviews.storage.filters import ENRICHED, PRESS

logger = logging.getLogger(__name__)

#: Articles transmis au modèle, au maximum.
#:
#: Six suffisent : au-delà, on paie des jetons pour une queue de distribution
#: que le modèle ne citera pas, et on augmente le risque qu'il retienne
#: l'article le plus spectaculaire plutôt que le plus proche du pic.
_MAX_ARTICLES = 6

#: En dessous de ce nombre, on élargit le périmètre plutôt que de se contenter
#: de ce qu'on a. Un seul article ne permet pas au modèle d'écarter une
#: coïncidence — et un article isolé rattaché à une filiale est plus souvent un
#: résidu d'attribution qu'un événement.
_MIN_AVANT_ELARGISSEMENT = 2

#: Caractères d'extrait transmis par article. Les flux RSS ne fournissent qu'un
#: titre et deux lignes ; tronquer plus court retirerait le peu de contexte
#: disponible.
_EXTRAIT_CHARS = 300


class PressRepository:
    """Articles de presse candidats à l'explication d'un mouvement d'opinion."""

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ Public

    def evidence(
        self,
        *,
        window: tuple[date, date],
        level: str,
        value: Optional[str],
        limit: int = _MAX_ARTICLES,
    ) -> dict[str, Any]:
        """Articles datés du périmètre le plus fin qui en porte.

        Args:
            window: fenêtre de recherche, amorce comprise. C'est à l'appelant
                de l'élargir vers l'amont : ce module ne décide pas de la
                latence entre un événement et sa traduction en avis.
            level: maille de l'entité analysée (`subsidiary`, `operator`,
                `country`, `region`). Toute autre valeur est traitée comme un
                périmètre global plutôt que de lever : une explication sans
                preuve reste une réponse acceptable, une erreur 500 non.
            value: identifiant de l'entité au sens du CONTRAT DE FILTRE — un
                pays se désigne par son ISO alpha-2. `None` cherche sur tout
                le périmètre.

        Returns:
            `articles` (les candidats), `perimetre` (la maille réellement
            retenue, en français, destinée au modèle ET à l'écran), `elargi`
            (vrai si l'on a dû sortir de la maille demandée).
        """
        demandee = _MAILLE_DEMANDEE.get(level, "tout le périmètre analysé")
        dernier: dict[str, Any] = {
            "articles": [],
            "perimetre": demandee,
            "elargi": False,
        }
        for maille, clause, params in self._chaine(level, value):
            articles = self._articles(window, clause, params, limit)
            dernier = {
                "articles": articles,
                "perimetre": maille,
                "elargi": maille != demandee,
            }
            if len(articles) >= _MIN_AVANT_ELARGISSEMENT:
                return dernier
        # Aucun périmètre n'a atteint le seuil : on renvoie le dernier tenté,
        # fût-il vide. Un « aucun article » explicite vaut mieux qu'une clé
        # absente, qui laisserait le modèle supposer qu'on n'a pas cherché.
        return dernier

    # ------------------------------------------------------------ Périmètres

    def _chaine(
        self, level: str, value: Optional[str]
    ) -> list[tuple[str, str, list[Any]]]:
        """Périmètres à tenter, du plus fin au plus large.

        L'élargissement s'arrête au PAYS. Aller jusqu'à la région ferait
        expliquer un pic ivoirien par un article kényan — la proximité
        géographique n'est pas une proximité de cause.
        """
        if value is None:
            return [("tout le périmètre analysé", "TRUE", [])]

        if level == "subsidiary":
            ids = self._parents(value)
            if ids is None:
                # Identifiant illisible ou filiale inconnue : on ne fabrique pas
                # une requête sur une valeur non entière, on cherche large.
                return [("tout le périmètre analysé", "TRUE", [])]
            _operator_id, iso2 = ids
            chaine = [("cette filiale", f"{ENRICHED.subsidiary_id} = %s", [int(value)])]
            # On élargit au PAYS, jamais à l'opérateur toutes filiales confondues.
            #
            # Mesuré, et c'est la raison de ce choix : Vodacom Tanzanie n'a
            # aucune presse propre ; l'élargissement par opérateur lui a remonté
            # « New rules of SA radio spectrum » et « WhatsApp scammers », c'est-
            # à-dire l'actualité SUD-AFRICAINE, offerte comme cause candidate à
            # un mouvement TANZANIEN. Une décision de régulateur, une panne, une
            # hausse tarifaire sont des faits NATIONAUX : le pays est le bon
            # élargissement, le groupe ne l'est pas.
            if iso2:
                # ÉLARGIR AU PAYS, MAIS PAS AUX CONCURRENTS.
                #
                # Mesuré : sans cette exclusion, une alerte sur Glo Nigeria se
                # voyait proposer « MTN Nigeria's growth engine stalled », e&
                # Égypte un article sur Vodafone, Telkom South Africa un
                # article sur Vodacom. Les résultats d'un concurrent
                # n'expliquent pas le mécontentement d'un opérateur — ils le
                # contredisent même parfois.
                #
                # On garde donc, à la maille pays, les articles NON RATTACHÉS
                # (décision de régulateur, panne nationale, hausse tarifaire
                # généralisée — 22 % du corpus) et ceux qui portent sur cette
                # filiale précise. Tout ce qui est attribué à une AUTRE filiale
                # est écarté.
                chaine.append(
                    (
                        "ce pays, hors actualité des concurrents",
                        f"{ENRICHED.iso2} = %s AND "
                        f"({ENRICHED.subsidiary_id} IS NULL "
                        f"OR {ENRICHED.subsidiary_id} = %s)",
                        [iso2, int(value)],
                    )
                )
            return chaine

        if level == "operator":
            return [
                (
                    "cet opérateur, tous pays confondus",
                    f"{ENRICHED.operator_id} = %s",
                    [int(value)],
                )
            ]
        if level == "country":
            return [
                ("ce pays, tous opérateurs confondus", f"{ENRICHED.iso2} = %s", [str(value)])
            ]
        if level == "region":
            return [("cette région", f"{ENRICHED.region} = %s", [str(value)])]
        return [("tout le périmètre analysé", "TRUE", [])]

    def _parents(self, subsidiary_id: Any) -> Optional[tuple[Optional[int], Optional[str]]]:
        """Opérateur et pays d'une filiale, pour savoir vers quoi élargir."""
        try:
            casted = int(subsidiary_id)
        except (TypeError, ValueError):
            return None
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT s.operator_id, c.iso2 FROM dim_subsidiary s "
                "LEFT JOIN dim_country c ON c.country_id = s.country_id "
                "WHERE s.subsidiary_id = %s",
                (casted,),
            )
            row = cur.fetchone()
        return (row[0], row[1]) if row else None

    # -------------------------------------------------------------- Requête

    def _articles(
        self, window: tuple[date, date], clause: str, params: list[Any], limit: int
    ) -> list[dict]:
        """Articles du périmètre, dédoublonnés et re-filtrés sur la pertinence.

        Le filtre de pertinence est REPASSÉ ICI alors que `v_reviews_enriched`
        écarte déjà l'attribution « noise ». Ce n'est pas redondant : mesuré, 8
        articles du corpus visible échappent au tri d'attribution tout en étant
        hors sujet — une nécrologie et un barrage rattachés à Vodacom South
        Africa. Huit sur 2 350 est négligeable dans un agrégat, mais ce module
        n'en transmet que six : un seul suffirait à faire expliquer une chute de
        satisfaction par le niveau d'un barrage.
        """
        start, end = window
        # LA BONNE NOUVELLE N'EXPLIQUE PAS LA MAUVAISE.
        #
        # Sans ce filtre, la sélection se faisait sur la seule date, et
        # proposait comme cause possible d'une chute de satisfaction des
        # articles tels que « inwi renforce la couverture mobile » ou
        # « Chinguitel lance la 5G ». Une amélioration annoncée n'explique pas
        # un mécontentement : la juxtaposition n'était pas seulement inutile,
        # elle décrédibilisait tout le bloc.
        #
        # Le neutre est CONSERVÉ, et c'est délibéré : une décision de
        # régulateur ou une hausse tarifaire est rédigée sans affect et sort
        # neutre du lexique, alors que c'est précisément le genre d'événement
        # qu'on cherche. Mesuré, le corpus visible est neutre à 87,6 % — ne
        # garder que le négatif (6,1 %) réduirait la couverture à presque rien.
        #
        # Le NÉGATIF PASSE DEVANT à date comparable : entre une panne et un
        # communiqué anodin du même jour, la panne est le meilleur candidat.
        sql = f"""
            SELECT v.title, v.text,
                   ({ENRICHED.occurred_at})::date AS date_article,
                   v.source AS media, v.subsidiary, v.country, v.sentiment
            FROM v_reviews_enriched v
            WHERE {ENRICHED.source_kind} = %s
              AND {ENRICHED.occurred_at} >= %s
              AND {ENRICHED.occurred_at} < %s
              AND COALESCE(v.sentiment, 'neutral') <> 'positive'
              AND ({clause})
            ORDER BY (v.sentiment = 'negative') DESC,
                     {ENRICHED.occurred_at} DESC
        """
        try:
            with self.db.cursor(dict_rows=True) as cur:
                cur.execute(sql, [PRESS, start, end, *params])
                lignes = cur.fetchall()
        except Exception:  # noqa: BLE001
            # La presse est un ENRICHISSEMENT de l'explication, pas son socle.
            # Si elle échoue, la synthèse doit sortir sans elle plutôt que
            # d'entraîner l'écran entier dans sa chute.
            logger.warning("Preuves de presse illisibles.", exc_info=True)
            return []

        articles: list[dict] = []
        vus: set[str] = set()
        for ligne in lignes:
            titre = (ligne["title"] or "").strip()
            texte = (ligne["text"] or "").strip()
            if not titre or not est_pertinent(titre, texte):
                continue
            # Google News renvoie le même événement repris par plusieurs médias.
            # Sans dédoublonnage, les six places disponibles peuvent être
            # occupées par un seul fait, et le modèle lit une insistance là où
            # il n'y a qu'une reprise.
            cle = titre.lower()[:80]
            if cle in vus:
                continue
            vus.add(cle)
            articles.append(
                {
                    "date": ligne["date_article"].isoformat()
                    if ligne["date_article"]
                    else None,
                    "titre": titre[:_EXTRAIT_CHARS],
                    "media": ligne["media"],
                    "filiale_citee": ligne["subsidiary"],
                    "pays": ligne["country"],
                    # Transmis à l'appelant : le modèle doit pouvoir dire
                    # « une panne rapportée le 4 août » plutôt que de traiter
                    # un communiqué neutre avec la même force.
                    "tonalite": ligne.get("sentiment"),
                }
            )
            if len(articles) >= limit:
                break
        return articles


#: Maille nominale de chaque niveau, pour savoir si l'on a dû s'en écarter.
_MAILLE_DEMANDEE = {
    "subsidiary": "cette filiale",
    "operator": "cet opérateur, tous pays confondus",
    "country": "ce pays, tous opérateurs confondus",
    "region": "cette région",
}
