"""
Classe de base des collecteurs.

Responsabilité UNIQUE : aller chercher des avis et les retourner.
La validation métier est faite par le modèle Review à la construction ;
le calcul du sentiment, la déduplication et la persistance sont du ressort
du pipeline (reviews/pipeline/runner.py). Un collecteur ne touche jamais la BD.
"""

from abc import ABC, abstractmethod
import logging
from datetime import datetime, timedelta
from typing import Optional

from reviews.domain.models import Review, ScraperResult
from reviews.processing.resilience import RetryConfig, execute_with_retry


class CollectorBackoff(Exception):
    """La SOURCE demande de lever le pied — ce n'est pas un échec de l'unité.

    Levée par un collecteur bridé par sa source (GDELT limite le débit et
    finit par tout refuser). Le pilote du mode unités doit alors REMETTRE
    l'unité en file sans compter de tentative, et arrêter le passage.

    Sans cette distinction, un bridage se propageait en cascade : chaque unité
    tentée pendant la fenêtre de bridage consommait une tentative, et les 132
    filiales de GDELT arrivaient à trois tentatives — donc au statut « failed »
    — en un seul passage, pour une raison qui n'avait rien à voir avec elles.
    """


class BaseCollector(ABC):
    """Collecteur abstrait. Sous-classer et implémenter collect()."""

    # False pour les collecteurs Playwright (API sync liée au thread OS) :
    # pas de timeout par thread worker (voir execute_with_retry).
    USES_THREAD_TIMEOUT = True

    # Repère de collecte incrémentale : {(company, source): date du dernier
    # avis en base}. Injecté par le pipeline APRÈS construction — un collecteur
    # ne lit jamais la base lui-même. Vide par défaut : le collecteur reste
    # utilisable seul (tests, exécution manuelle) et collecte alors tout.
    since: dict[tuple[str, str], datetime] = {}

    # Marge de sécurité appliquée au repère. Les sources n'indexent pas toujours
    # dans l'ordre chronologique : un avis publié hier peut n'apparaître
    # qu'aujourd'hui. Sans marge, il serait définitivement ignoré. Les avis
    # re-proposés dans la marge sont écartés sans coût par ON CONFLICT.
    INCREMENTAL_MARGIN = timedelta(days=2)

    def __init__(self, name: str, retry_config: Optional[RetryConfig] = None):
        self.name = name
        self.retry_config = retry_config or RetryConfig()
        self.logger = logging.getLogger(f"collector.{name}")

    def _cutoff_for(self, review: Review) -> Optional[datetime]:
        """Date en deçà de laquelle cet avis est déjà connu en base.

        La clé s'appuie sur `review.source` et NON sur `self.name` : le nom du
        collecteur diffère de la valeur stockée en base ("appstore" vs
        "app_store", "playstore" vs "google_play", "googlemaps" vs
        "google_maps"). Utiliser self.name ne remontait jamais de repère et
        désactivait silencieusement tout l'incrémental.
        """
        source = getattr(review.source, "value", review.source)
        return self.cutoff_for_key(review.company, source, review.target_id)

    def cutoff_for_key(self, company: str, source: str,
                       target: Optional[str] = None) -> Optional[datetime]:
        """Même repère, accessible AVANT d'avoir construit un objet Review.

        C'est ce qui permet aux collecteurs d'interrompre leur pagination en
        cours de route : à ce stade ils ne manipulent que des dictionnaires
        bruts, pas encore des Review.

        `target` DÉSIGNE LA SOUS-CIBLE — une agence Google Maps, une
        application. Le repère est propre à chacune : sans cela, l'agence la
        plus active fixerait la date pour toute la filiale et les agences moins
        actives seraient intégralement écartées comme « déjà connues ».

        Une sous-cible jamais collectée n'a pas de repère, donc pas de coupure,
        donc elle est collectée en entier — le comportement voulu à sa
        découverte.
        """
        last = self.since.get((company, source, target))
        return last - self.INCREMENTAL_MARGIN if last else None

    @staticmethod
    def is_already_known(created: Optional[datetime],
                         cutoff: Optional[datetime]) -> bool:
        """Cet avis est-il antérieur au repère, donc déjà en base ?

        Comparaison sûre : la base renvoie des dates avec fuseau, les
        collecteurs en produisent parfois des naïves. On aligne sur le naïf
        plutôt que de risquer un TypeError en pleine collecte.

        Règle unique, partagée par le filtrage final et par l'arrêt anticipé
        des collecteurs : deux comparaisons différentes finiraient par diverger
        d'un demi-jour et le pipeline perdrait des avis sans rien signaler.
        """
        if not created or not cutoff:
            return False
        if created.tzinfo and not cutoff.tzinfo:
            created = created.replace(tzinfo=None)
        elif cutoff.tzinfo and not created.tzinfo:
            cutoff = cutoff.replace(tzinfo=None)
        return created <= cutoff

    def batch_fully_known(self, dates: list[Optional[datetime]],
                          cutoff: Optional[datetime]) -> bool:
        """Un lot entier est-il déjà connu ? Si oui, inutile de paginer plus loin.

        À n'utiliser QUE sur une source triée du plus récent au plus ancien —
        c'est le cas d'App Store (`sortby=mostrecent`), de Google Play
        (`Sort.NEWEST`) et de Google Maps depuis le passage au tri par date.
        Sur une source non triée, un lot ancien ne dit rien de ce qui suit et
        l'arrêt ferait perdre des avis en silence.

        On exige que le lot soit ENTIÈREMENT connu, et non qu'il contienne un
        seul avis connu : les sources n'indexent pas toujours dans un ordre
        parfait, et un avis republié plus bas ne doit pas interrompre la
        collecte de ceux qui le suivent.
        """
        if cutoff is None or not dates:
            return False
        return all(self.is_already_known(d, cutoff) for d in dates)

    def drop_already_known(self, reviews: list[Review]) -> list[Review]:
        """Filet incrémental, exposé au pilote du mode unités.

        `run()` l'applique lui-même en mode classique ; quand c'est le pipeline
        qui enchaîne les unités, c'est à lui de l'appeler — sans quoi chaque
        unité réinsérerait ce qui est déjà en base.
        """
        return self._filter_already_known(reviews)

    def _filter_already_known(self, reviews: list[Review]) -> list[Review]:
        """Écarte les avis antérieurs au repère (déjà en base).

        Reste le filet de sécurité FINAL, même quand un collecteur s'arrête
        désormais plus tôt : l'arrêt anticipé évite de télécharger, il ne
        garantit pas que le dernier lot ne contienne aucun avis déjà connu.
        """
        if not self.since:
            return reviews

        kept, skipped = [], 0
        for review in reviews:
            if self.is_already_known(review.created_at, self._cutoff_for(review)):
                skipped += 1
                continue
            kept.append(review)

        if skipped:
            self.logger.info(
                "%d avis déjà en base écartés avant insertion (%d retenus)",
                skipped, len(kept),
            )
        return kept

    @abstractmethod
    def collect(self) -> list[Review]:
        """Collecte et retourne les avis bruts (déjà en objets Review)."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # COLLECTE PAR UNITÉS — file `collection_jobs`
    #
    # POURQUOI CE SECOND MODE EXISTE
    #   `collect()` rend TOUT ou RIEN : le pipeline n'insère qu'après son
    #   retour. Google Maps y enchaînait 405 recherches sur une dizaine
    #   d'heures, si bien qu'une interruption perdait l'intégralité du travail
    #   et qu'une erreur à la 200e condamnait les 205 suivantes, pourtant
    #   indépendantes.
    #
    #   Un collecteur qui déclare `SUPPORTS_UNITS = True` est piloté unité par
    #   unité par le pipeline, qui persiste après CHACUNE. Un échec ne coûte
    #   alors que son unité.
    #
    # CE MODE EST FACULTATIF. Les collecteurs rapides — un flux RSS met 76 s —
    # n'y gagneraient rien et gardent `collect()`. Le pipeline choisit selon ce
    # drapeau, sans savoir lequel est lequel.
    # ------------------------------------------------------------------

    #: Le collecteur sait-il travailler par unités reprenables ?
    SUPPORTS_UNITS = False

    def plan_units(self) -> list[dict]:
        """Catalogue des unités à collecter.

        Chaque dict porte au minimum `job_key` — identité STABLE de l'unité,
        indépendante de son rang — et facultativement `company`, `operator`,
        `country`, `location`, `query`, `priority`.
        """
        raise NotImplementedError

    def open_session(self) -> None:
        """Ouvre ce qui est coûteux et partagé entre unités (un navigateur).

        Sans ce point d'accroche, chaque unité relancerait Playwright : deux à
        trois secondes de démarrage par unité, soit une vingtaine de minutes
        gaspillées sur 405 — le découpage coûterait plus qu'il ne rapporte.
        """

    def close_session(self) -> None:
        """Referme ce qu'a ouvert `open_session`. Toujours appelée."""

    def collect_unit(self, job, save_cursor) -> list[Review]:
        """Collecte UNE unité et retourne ses avis.

        `save_cursor(dict)` enregistre l'avancement EN COURS d'unité, pour
        reprendre après interruption. Appeler au fil de l'eau : un curseur
        écrit à la fin ne sert à rien, l'interruption arrive avant.
        """
        raise NotImplementedError

    def run(self) -> ScraperResult:
        """Lance la collecte avec retry/timeout. NE persiste rien.

        Retourne un ScraperResult dont `reviews` contient les avis collectés,
        que le pipeline enrichira (sentiment) puis persistera.
        """
        result = ScraperResult(
            scraper_name=self.name,
            reviews=[],
            started_at=datetime.utcnow(),
            status="running",
        )
        try:
            self.logger.info(f"Démarrage de {self.name}")
            reviews = execute_with_retry(
                func=self.collect,
                retry_config=self.retry_config,
                logger=self.logger,
                use_thread_timeout=self.USES_THREAD_TIMEOUT,
            ) or []

            reviews = self._filter_already_known(reviews)
            result.reviews = reviews
            result.ended_at = datetime.utcnow()
            result.status = "success"
            self.logger.info(f"{self.name} : {len(reviews)} avis collectés")
            return result
        except Exception as e:
            self.logger.error(f"Erreur dans {self.name} : {e}", exc_info=True)
            result.ended_at = datetime.utcnow()
            result.status = "failed"
            result.error_message = str(e)
            return result
