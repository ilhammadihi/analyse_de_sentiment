"""
Module 1 — couverture : ce qu'on DEVRAIT avoir, face à ce qu'on A.

CE QUI MANQUAIT VRAIMENT, ET QUI N'ÉTAIT PAS LA MESURE
    Le dépôt sait déjà compter. `v_subsidiary_volume` donne les avis par
    filiale, `v_target_coverage` les sous-cibles couvertes par source,
    `collection_jobs` l'état unité par unité, `run_metrics` le résultat de
    chaque passage. Rien de tout cela n'est réécrit ici.

    Ce qui manquait est le DÉNOMINATEUR : la notion de « source ATTENDUE pour
    cette filiale ». Elle n'existait qu'implicitement, dans `operators.json`, et
    aucune requête ne pouvait la lire.

    Sans elle, un zéro est muet. « Orange Mali : 0 avis App Store » ne dit pas
    s'il s'agit d'une panne ou du fait, parfaitement normal, qu'Orange Mali ne
    publie aucune application iOS. Mesuré sur le périmètre : 86 filiales sur
    135 déclarent une application App Store, 89 une application Play Store.
    Compter les 49 autres comme « non couvertes App Store » produirait 49
    fausses anomalies permanentes — la faute Trustpilot, à l'échelle du
    périmètre entier.

LE POINT DE VÉRITÉ EST `config/operators.json`, PAS LA BASE
    C'est là que se déclare ce qu'on cherche à collecter, et `targets.py` sait
    déjà le lire. On le réutilise tel quel plutôt que de dupliquer la lecture :
    une seconde source de vérité sur « quelles filiales collecter » divergerait
    au premier ajout d'opérateur.

TROIS ESPACES DE NOMMAGE, ET C'EST UN PIÈGE RÉEL
    Le même concept porte trois noms selon l'endroit :

        operators.json   dim_source     collecteur
        appstore     ->  app_store   -> appstore
        playstore    ->  google_play -> playstore
        google_maps  ->  google_maps -> googlemaps
        rss          ->  rss_feed    -> rss_feed

    Les confondre ne lève aucune erreur : cela rend simplement une couverture
    nulle pour une source pourtant collectée. La table de correspondance est
    donc déclarée une fois, ici, et tout le module s'y réfère.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from reviews.collectors.targets import load_subsidiaries
from reviews.storage.db import Database

logger = logging.getLogger(__name__)


#: Correspondance `operators.json` -> code `dim_source`, pour les seules
#: sources d'AVIS CLIENTS déclarées par filiale.
#:
#: `rss` en est volontairement ABSENT : il produit `rss_feed`, qui est de la
#: PRESSE (`kind='press'`). L'inclure ferait entrer 7 787 articles dans un
#: décompte de satisfaction, exactement le mélange que la migration 002 a
#: séparé et que tout le projet s'interdit depuis.
SOURCES_ATTENDUES: dict[str, str] = {
    "appstore": "app_store",
    "playstore": "google_play",
    "google_maps": "google_maps",
    "trustpilot": "trustpilot",
    "hellopeter": "hellopeter",
}

#: Code `dim_source` -> nom du collecteur, pour rapprocher une source de la
#: file `collection_jobs` et des `run_metrics`, qui parlent tous deux en noms
#: de collecteurs.
SOURCE_VERS_COLLECTEUR: dict[str, str] = {
    "app_store": "appstore",
    "google_play": "playstore",
    "google_maps": "googlemaps",
    "trustpilot": "trustpilot",
    "hellopeter": "hellopeter",
    "rss_feed": "rss_feed",
    "gdelt": "gdelt",
    "press_feed": "press_feed",
    "reddit": "reddit",
}

#: Sources d'avis clients qui ne se DÉCLARENT pas par filiale.
#:
#: Reddit interroge un subreddit PAYS et rattache ensuite les fils aux filiales
#: citées (voir `reddit_targets`). Aucune filiale ne peut donc « déclarer »
#: Reddit, et une filiale sans avis Reddit n'est en défaut de rien. Ses avis
#: comptent en revanche pleinement quand ils existent : la source est un bonus
#: observé, jamais une attente.
SOURCES_OPPORTUNISTES: frozenset[str] = frozenset({"reddit"})


@dataclass
class EtatSource:
    """Ce qu'une source a produit pour une filiale, et ce qu'elle en dit."""

    code: str
    attendue: bool = False
    avis: int = 0
    avis_recents: int = 0
    derniere_collecte: Optional[datetime] = None
    dernier_avis: Optional[datetime] = None

    #: État de la file `collection_jobs`, quand la source y est découpée en
    #: unités. Ces trois compteurs sont ce qui permet au diagnostic de séparer
    #: « jamais tenté » de « tenté et vide » — la distinction la plus utile du
    #: module, et celle qu'aucune mesure existante ne donnait.
    #: Unités actuellement en succès. ÉTAT TRANSITOIRE, à ne jamais utiliser
    #: pour conclure qu'une source a été exécutée : `reschedule_due` repasse en
    #: `pending` toute unité dont la cadence est écoulée. Conservé pour
    #: l'affichage, pas pour le diagnostic.
    unites_succes: int = 0

    #: Unités ayant DÉJÀ abouti au moins une fois (`last_success_at` non nul).
    #:
    #: C'EST LE SEUL FAIT DURABLE, et la distinction n'est pas théorique : les
    #: six unités Google Maps de Comores Telecom sont toutes en `pending` avec
    #: `last_success_at` renseigné et `items_inserted = 0`. Lues sur leur
    #: statut, elles disent « jamais tentée » et l'agent recommande d'attendre ;
    #: lues sur leur dernier succès, elles disent « interrogée avec succès, et
    #: vide » — et l'agent recommande de chercher ailleurs. Deux diagnostics
    #: opposés, deux actions incompatibles, sur les mêmes lignes.
    unites_deja_reussies: int = 0

    unites_echec: int = 0
    unites_attente: int = 0
    unites_jamais_reussies: int = 0
    items_inserted: int = 0
    derniere_erreur: Optional[str] = None

    @property
    def a_des_avis(self) -> bool:
        return self.avis > 0

    @property
    def tentee(self) -> bool:
        """La source a-t-elle réellement été exécutée pour cette filiale ?

        Sur `unites_deja_reussies` OU des avis : une source non découpée en
        unités (Play Store, App Store) n'a pas de ligne dans `collection_jobs`,
        et son seul témoignage d'exécution est le résultat. Exiger les unités
        déclarerait « jamais tentées » les deux sources les plus productives du
        corpus.
        """
        return self.unites_deja_reussies > 0 or self.avis > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.code,
            "attendue": self.attendue,
            "avis": self.avis,
            "avis_recents": self.avis_recents,
            "derniere_collecte": (
                self.derniere_collecte.isoformat() if self.derniere_collecte else None
            ),
            "unites": {
                "succes": self.unites_succes,
                "deja_reussies": self.unites_deja_reussies,
                "echec": self.unites_echec,
                "attente": self.unites_attente,
                "jamais_reussies": self.unites_jamais_reussies,
            },
            "items_inserted": self.items_inserted,
            "derniere_erreur": self.derniere_erreur,
        }


@dataclass
class CouvertureFiliale:
    """Couverture d'une filiale, source par source."""

    subsidiary_id: int
    subsidiary: str
    operator: str
    country: str
    iso2: str

    avis_clients: int = 0
    avis_recents: int = 0
    articles_presse: int = 0
    derniere_collecte: Optional[datetime] = None
    dernier_avis: Optional[datetime] = None

    sources: dict[str, EtatSource] = field(default_factory=dict)

    # --- Lectures dérivées ------------------------------------------------

    @property
    def sources_attendues(self) -> list[str]:
        return sorted(c for c, s in self.sources.items() if s.attendue)

    @property
    def sources_actives(self) -> list[str]:
        """Sources ayant réellement produit des avis."""
        return sorted(c for c, s in self.sources.items() if s.a_des_avis)

    @property
    def sources_muettes(self) -> list[str]:
        """Attendues, exécutées, et pourtant sans le moindre avis.

        C'EST LA LISTE QUI COMPTE POUR LE DIAGNOSTIC. Une source attendue mais
        jamais exécutée n'est pas muette, elle est en attente — et les deux
        appellent des actions opposées.
        """
        return sorted(
            c for c, s in self.sources.items()
            if s.attendue and s.tentee and not s.a_des_avis
        )

    @property
    def sources_en_erreur(self) -> list[str]:
        """Attendues, et dont aucune unité n'a jamais réussi malgré des essais.

        `unites_jamais_reussies` et non `unites_echec` : une unité qui échoue
        aujourd'hui mais a déjà réussi hier ne signale pas une panne, elle
        signale une page lente. La leçon est celle de `collection_jobs` — seules
        les unités qui n'ont JAMAIS abouti sont réellement inquiétantes.
        """
        return sorted(
            c for c, s in self.sources.items()
            if s.attendue and s.unites_jamais_reussies > 0
            and s.unites_deja_reussies == 0
        )

    @property
    def sources_jamais_tentees(self) -> list[str]:
        return sorted(
            c for c, s in self.sources.items() if s.attendue and not s.tentee
        )

    @property
    def taux_couverture_sources(self) -> Optional[float]:
        """Part des sources attendues qui produisent réellement des avis.

        `None` quand AUCUNE source n'est attendue. Ce n'est pas zéro : une
        filiale pour laquelle on n'a rien déclaré n'a pas une couverture nulle,
        elle a une couverture indéfinie. Rendre 0,0 la ferait apparaître en tête
        des filiales « en défaut » alors que le défaut est dans notre
        configuration, pas dans la collecte — et c'est un tout autre travail.
        """
        attendues = self.sources_attendues
        if not attendues:
            return None
        actives = sum(1 for c in attendues if self.sources[c].a_des_avis)
        return actives / len(attendues)

    def as_dict(self) -> dict[str, Any]:
        return {
            "subsidiary_id": self.subsidiary_id,
            "subsidiary": self.subsidiary,
            "operator": self.operator,
            "country": self.country,
            "iso2": self.iso2,
            "avis_clients": self.avis_clients,
            "avis_recents": self.avis_recents,
            "articles_presse": self.articles_presse,
            "derniere_collecte": (
                self.derniere_collecte.isoformat() if self.derniere_collecte else None
            ),
            "sources_attendues": self.sources_attendues,
            "sources_actives": self.sources_actives,
            "sources_muettes": self.sources_muettes,
            "sources_en_erreur": self.sources_en_erreur,
            "sources_jamais_tentees": self.sources_jamais_tentees,
            "taux_couverture_sources": self.taux_couverture_sources,
            "detail": [s.as_dict() for s in self.sources.values()],
        }


class MoniteurCouverture:
    """Assemble la couverture de toutes les filiales, en quatre requêtes.

    QUATRE REQUÊTES POUR 135 FILIALES, ET NON QUATRE PAR FILIALE. Une boucle
    Python appelant la base par filiale ferait 540 allers-retours à chaque
    passage. Ce module tourne dans le même processus que la collecte : il doit
    coûter des millisecondes, sinon il devient lui-même le problème qu'il
    surveille.
    """

    def __init__(self, db: Database, fenetre_jours: int = 30):
        self.db = db
        self.fenetre_jours = fenetre_jours

    # ------------------------------------------------------------------ Public

    def analyser(self) -> list[CouvertureFiliale]:
        """Couverture de toutes les filiales actives, la plus pauvre d'abord."""
        filiales = self._filiales()
        declarations = self._declarations()

        for cle, sub in filiales.items():
            for code in declarations.get(sub.subsidiary.lower(), set()):
                sub.sources.setdefault(code, EtatSource(code=code)).attendue = True
            # La correspondance se fait sur le NOM de la filiale, seul lien
            # entre `operators.json` (qui n'a pas d'identifiant de base) et
            # `dim_subsidiary`. Un nom présent d'un seul côté est un écart de
            # configuration réel : on le journalise plutôt que de le taire,
            # c'est exactement le genre de trou qui rend une couverture
            # faussement bonne.
            del cle

        self._remplir_avis(filiales)
        self._remplir_jobs(filiales)
        self._remplir_runs(filiales)

        manquantes = set(declarations) - {
            s.subsidiary.lower() for s in filiales.values()
        }
        if manquantes:
            logger.warning(
                "Couverture : %d filiale(s) déclarée(s) dans operators.json et "
                "absente(s) de dim_subsidiary : %s",
                len(manquantes), ", ".join(sorted(manquantes)[:5]),
            )

        return sorted(filiales.values(), key=lambda s: s.avis_clients)

    # -------------------------------------------------------------- Chargement

    def _declarations(self) -> dict[str, set[str]]:
        """Sources d'avis clients déclarées, par nom de filiale en minuscules.

        Lit `operators.json` par `load_subsidiaries()`, qui est déjà le point
        de vérité du projet et met son résultat en cache.
        """
        out: dict[str, set[str]] = {}
        for sub in load_subsidiaries():
            nom = (sub.get("subsidiary_name") or "").strip()
            if not nom:
                continue
            declarees = set()
            for cle, code in SOURCES_ATTENDUES.items():
                if (sub.get("sources") or {}).get(cle):
                    declarees.add(code)
            out[nom.lower()] = declarees
        return out

    def _filiales(self) -> dict[int, CouvertureFiliale]:
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT sub.subsidiary_id, sub.name AS subsidiary,
                       op.name AS operator, co.name AS country, co.iso2
                FROM dim_subsidiary sub
                JOIN dim_operator op ON op.operator_id = sub.operator_id
                JOIN dim_country  co ON co.country_id  = sub.country_id
                WHERE sub.active
                ORDER BY sub.name
                """
            )
            return {
                r["subsidiary_id"]: CouvertureFiliale(**dict(r))
                for r in cur.fetchall()
            }

    def _remplir_avis(self, filiales: dict[int, CouvertureFiliale]) -> None:
        """Volumes et dates observés, par filiale et par source.

        Le décompte des AVIS CLIENTS écarte la presse, comme partout ailleurs :
        les 7 787 articles RSS écraseraient numériquement les avis et feraient
        passer pour couverte une filiale dont aucun client ne s'est exprimé.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT r.subsidiary_id, s.code, s.kind,
                       COUNT(*)                                   AS avis,
                       COUNT(*) FILTER (
                           WHERE COALESCE(r.created_at, r.collected_at)
                                 >= now() - make_interval(days => %s)
                       )                                          AS recents,
                       MAX(r.collected_at)                        AS derniere_collecte,
                       MAX(COALESCE(r.created_at, r.collected_at)) AS dernier_avis
                FROM reviews r
                JOIN dim_source s ON s.source_id = r.source_id
                WHERE r.subsidiary_id IS NOT NULL
                GROUP BY r.subsidiary_id, s.code, s.kind
                """,
                (self.fenetre_jours,),
            )
            for row in cur.fetchall():
                filiale = filiales.get(row["subsidiary_id"])
                if filiale is None:
                    continue
                if row["kind"] == "press":
                    filiale.articles_presse += row["avis"]
                    # La presse ne compte NI dans les avis clients, NI comme
                    # source active. Elle reste lisible dans le détail parce
                    # qu'elle est le signe qu'une entité EXISTE et qu'on sait la
                    # reconnaître — l'argument décisif du diagnostic sur les
                    # trois filiales à zéro avis, qui portent 42, 18 et 2
                    # articles.
                    continue

                filiale.avis_clients += row["avis"]
                filiale.avis_recents += row["recents"]
                etat = filiale.sources.setdefault(
                    row["code"], EtatSource(code=row["code"])
                )
                etat.avis = row["avis"]
                etat.avis_recents = row["recents"]
                etat.derniere_collecte = row["derniere_collecte"]
                etat.dernier_avis = row["dernier_avis"]
                filiale.derniere_collecte = _plus_recent(
                    filiale.derniere_collecte, row["derniere_collecte"]
                )
                filiale.dernier_avis = _plus_recent(
                    filiale.dernier_avis, row["dernier_avis"]
                )

    def _remplir_jobs(self, filiales: dict[int, CouvertureFiliale]) -> None:
        """État de la file de collecte, par filiale et par source.

        C'EST LA REQUÊTE QUI REND LE DIAGNOSTIC POSSIBLE. Sans elle, on ne peut
        pas distinguer « la source a été interrogée et n'a rien rendu » de « la
        source n'a jamais été interrogée » — deux situations qui affichent
        toutes deux zéro avis et appellent l'une de chercher ailleurs, l'autre
        d'attendre le prochain passage.

        Le rapprochement se fait sur `company`, seul lien entre la file et les
        dimensions : `collection_jobs` porte volontairement son contexte métier
        dénormalisé (migration 012) et n'a pas de `subsidiary_id`.
        """
        par_nom = {f.subsidiary.lower(): f for f in filiales.values()}
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT lower(company) AS nom, source,
                       COUNT(*) FILTER (WHERE status = 'success')          AS succes,
                       -- LE FAIT DURABLE. `status` est transitoire : une unité
                       -- réussie repasse en `pending` dès que sa cadence est
                       -- écoulée (`reschedule_due`). Compter sur le statut fait
                       -- passer une source interrogée avec succès pour une
                       -- source jamais tentée — voir `unites_deja_reussies`.
                       COUNT(*) FILTER (WHERE last_success_at IS NOT NULL) AS deja,
                       COUNT(*) FILTER (WHERE status = 'failed')           AS echec,
                       COUNT(*) FILTER (WHERE status IN ('pending','running')) AS attente,
                       COUNT(*) FILTER (
                           WHERE last_success_at IS NULL AND attempts > 0
                       )                                                   AS jamais,
                       COALESCE(SUM(items_inserted), 0)                    AS inserted,
                       MAX(last_success_at)                                AS dernier_succes,
                       MAX(error_message)                                  AS erreur
                FROM collection_jobs
                WHERE company IS NOT NULL
                GROUP BY lower(company), source
                """
            )
            for row in cur.fetchall():
                filiale = par_nom.get(row["nom"])
                if filiale is None:
                    continue
                code = _collecteur_vers_source(row["source"])
                if code is None:
                    continue
                etat = filiale.sources.setdefault(code, EtatSource(code=code))
                etat.unites_succes = row["succes"]
                etat.unites_deja_reussies = row["deja"]
                etat.unites_echec = row["echec"]
                etat.unites_attente = row["attente"]
                etat.unites_jamais_reussies = row["jamais"]
                etat.items_inserted = row["inserted"]
                etat.derniere_erreur = (row["erreur"] or None)
                etat.derniere_collecte = _plus_recent(
                    etat.derniere_collecte, row["dernier_succes"]
                )

    def _remplir_runs(self, filiales: dict[int, CouvertureFiliale]) -> None:
        """Dernier passage réussi de chaque collecteur, toutes filiales confondues.

        SERT DE REPLI, ET SEULEMENT DE REPLI. Play Store et App Store ne sont
        pas découpés en unités : leur seule trace d'exécution est `run_metrics`,
        qui est globale à la source et ne dit rien d'une filiale en
        particulier. On ne s'en sert donc que pour dater la dernière tentative
        d'une source dont aucune unité n'existe — jamais pour conclure qu'une
        filiale précise a été couverte.
        """
        with self.db.cursor(dict_rows=True) as cur:
            cur.execute(
                """
                SELECT scraper_name,
                       MAX(recorded_at) FILTER (WHERE status = 'success')
                           AS dernier_succes,
                       COALESCE(SUM(inserted_count), 0) AS inserted
                FROM run_metrics
                WHERE recorded_at >= now() - make_interval(days => %s)
                GROUP BY scraper_name
                """,
                (max(self.fenetre_jours, 30),),
            )
            par_source = {
                _collecteur_vers_source(r["scraper_name"]): dict(r)
                for r in cur.fetchall()
            }

        for filiale in filiales.values():
            for code, etat in filiale.sources.items():
                infos = par_source.get(code)
                # `unites_attente` à zéro ET aucune unité connue : la source
                # n'est pas découpée en unités pour cette filiale. C'est le seul
                # cas où `run_metrics` fait foi.
                if (
                    infos
                    and etat.unites_deja_reussies == 0
                    and etat.unites_attente == 0
                    and etat.unites_echec == 0
                ):
                    etat.derniere_collecte = _plus_recent(
                        etat.derniere_collecte, infos["dernier_succes"]
                    )
                    # La source a bien tourné globalement : on ne peut pas dire
                    # que cette filiale n'a jamais été tentée. Un passage réussi
                    # de la source vaut tentative, faute d'unité pour l'attester.
                    if infos["dernier_succes"] is not None and etat.attendue:
                        etat.unites_deja_reussies = 1


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

#: Inverse de SOURCE_VERS_COLLECTEUR, construit une seule fois.
_COLLECTEUR_VERS_SOURCE = {v: k for k, v in SOURCE_VERS_COLLECTEUR.items()}


def _collecteur_vers_source(nom: Optional[str]) -> Optional[str]:
    """« googlemaps » -> « google_maps ». None si le nom est inconnu.

    Rendre None plutôt que le nom brut est délibéré : un collecteur ajouté sans
    entrée dans la table de correspondance produirait sinon une source fantôme
    dans la couverture, avec zéro avis attendu et zéro observé — un écart
    inventé de toutes pièces.
    """
    if not nom:
        return None
    return _COLLECTEUR_VERS_SOURCE.get(nom)


def _plus_recent(
    a: Optional[datetime], b: Optional[datetime]
) -> Optional[datetime]:
    """La plus récente des deux dates, en tolérant les absences."""
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)
