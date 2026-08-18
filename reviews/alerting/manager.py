"""
Gestionnaire d'alerting : évalue les règles, persiste les alertes et les
notifie sur les canaux configurés. Point d'entrée unique pour le pipeline.
"""

import logging
from datetime import date, timedelta
from typing import Any, Optional

from reviews.config import AlertingConfig
from reviews.domain.models import PipelineRun, Alert
from reviews.alerting import rules
from reviews.alerting.notifiers import Notifier, build_notifiers
from reviews.storage.filters import StatsFilter
from reviews.storage.repository import AlertRepository
from reviews.storage.stats_repository import StatsRepository

logger = logging.getLogger(__name__)

#: Mois abrégés en français.
#:
#: Table en dur plutôt que `locale` : le conteneur ne porte aucune locale
#: française, et `setlocale` y échoue silencieusement en rendant des dates en
#: anglais. Douze entrées valent mieux qu'une dépendance système invisible.
_MOIS = ("janv.", "févr.", "mars", "avr.", "mai", "juin",
         "juil.", "août", "sept.", "oct.", "nov.", "déc.")


def _jour_court(valeur: Any) -> str:
    """« 8 août » à partir d'une date ou d'un horodatage. « ? » si illisible."""
    if valeur is None:
        return "?"
    try:
        return f"{valeur.day} {_MOIS[valeur.month - 1]}"
    except (AttributeError, IndexError, TypeError):
        return str(valeur)[:10]


def _date_iso(valeur: Any) -> Any:
    """Convertit une chaîne « AAAA-MM-JJ » en date, sinon rend la valeur telle
    quelle — `_jour_court` sait retomber sur ses pieds."""
    if isinstance(valeur, str):
        try:
            return date.fromisoformat(valeur[:10])
        except ValueError:
            return valeur
    return valeur

#: Avis joints à une alerte. Deux, datés.
#:
#: Ils viennent OBLIGATOIREMENT de la fenêtre du pic — même périmètre que la
#: mesure qui déclenche l'alerte. Citer un avis de mars sous un pic de la
#: semaine ferait douter du chiffre lui-même, et la date affichée est ce qui
#: permet au lecteur de le vérifier sans nous croire sur parole.
_MAX_EXTRAITS = 2

#: Caractères par extrait. Assez pour qu'une plainte soit compréhensible,
#: assez peu pour que trois d'entre elles tiennent sur un écran.
_EXTRAIT_CHARS = 180

#: Événements extérieurs joints à une alerte : UN SEUL.
#:
#: Le plus récent de la fenêtre, c'est-à-dire le plus proche du pic. Un seul
#: parce que la question posée est « que s'est-il passé ? » et non « que
#: raconte la presse ? » : deux titres obligent le lecteur à choisir lui-même
#: lequel compte, ce qui est précisément le travail qu'on lui épargne.
_MAX_EVENEMENTS = 1

#: Jours ajoutés AVANT la fenêtre du pic pour la recherche de presse.
#:
#: Aligné sur l'agent de veille, qui applique la même règle pour la même
#: raison : une cause précède son effet. Deux valeurs différentes pour la même
#: notion produiraient deux réponses divergentes sur le même incident.
_AMORCE_PRESSE_JOURS = 14


class AlertManager:
    """Orchestre règles → persistance → notification.

    Deux familles d'alertes, produites différemment :

      - TECHNIQUES : déduites du run lui-même, sans I/O (`rules.evaluate`).
      - MÉTIER : un pic d'insatisfaction ne peut PAS se lire dans le contenu
        d'un run. Un run collecte des avis publiés sur des décennies ; sa
        composition ne décrit aucune période. Le pic se mesure sur les DATES DE
        PUBLICATION, en comparant une fenêtre courte à la précédente — ce qui
        exige l'historique en base, donc un StatsRepository.

    Le calcul vit ici, la DÉCISION reste dans `rules`, qui demeure une fonction
    pure et testable sans base.
    """

    def __init__(
        self,
        cfg: AlertingConfig,
        alert_repo: Optional[AlertRepository] = None,
        notifiers: Optional[list[Notifier]] = None,
        stats_repo: Optional[StatsRepository] = None,
    ):
        self.cfg = cfg
        self.alert_repo = alert_repo
        self.stats_repo = stats_repo
        self.notifiers = notifiers if notifiers is not None else build_notifiers(cfg)

    def process(self, run: PipelineRun) -> list[Alert]:
        """Évalue les alertes d'un run, les notifie et les persiste."""
        alerts = rules.evaluate(run, self.cfg)
        alerts += self._business_alerts()

        for alert in alerts:
            channels: list[str] = []
            message_id: Optional[int] = None
            for n in self.notifiers:
                if not n.send(alert):
                    continue
                channels.append(n.name)
                # L'identifiant Telegram est relevé JUSTE APRÈS l'envoi : c'est
                # la seule prise permettant de retirer ce message plus tard, et
                # le notifieur ne le garde que jusqu'au message suivant.
                if n.name == "telegram":
                    message_id = getattr(n, "last_message_id", None)
            if self.alert_repo is not None:
                try:
                    self.alert_repo.insert(
                        alert, notified=channels, telegram_message_id=message_id
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("Persistance alerte échouée : %s", e)
        if alerts:
            logger.info("%d alerte(s) déclenchée(s) pour le run %s",
                        len(alerts), run.run_id)
        return alerts

    # ------------------------------------------------------------------

    def _business_alerts(self) -> list[Alert]:
        """Pics d'insatisfaction, mesurés sur une fenêtre courte.

        Réutilise `movers()`, le calcul qui alimente déjà le bloc « Ce qui a
        changé » du dashboard. Ce partage n'est pas une économie de code : il
        garantit qu'une alerte correspond TOUJOURS à quelque chose de visible à
        l'écran. Deux calculs séparés finiraient par diverger, et un
        responsable recevrait des alertes introuvables dans son dashboard.

        Ne lève jamais : une base indisponible doit dégrader l'alerting métier,
        pas faire échouer la collecte qui vient de réussir.
        """
        if not self.cfg.enabled or self.stats_repo is None:
            return []

        try:
            window = StatsFilter(days=self.cfg.spike_window_days)
            mouvements = self.stats_repo.movers(
                window,
                level="subsidiary",
                limit=50,
                min_reviews=self.cfg.min_reviews_for_ratio,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Détection de pic indisponible : %s", e)
            return []

        if not mouvements.get("available"):
            return []

        candidates = rules.negative_spike_alerts(mouvements["degraded"], self.cfg)
        retenues = [a for a in candidates if not self._recently_alerted(a)]

        # Les extraits sont ajoutés APRÈS le filtre anti-répétition : les
        # chercher pour une alerte qu'on va taire coûterait une requête par
        # filiale à chaque passage du pipeline, soit des dizaines par jour pour
        # rien.
        self._attacher_extraits(retenues, mouvements["degraded"])
        return retenues

    def _attacher_extraits(
        self, alerts: list[Alert], mouvements: list[dict[str, Any]]
    ) -> None:
        """Joint à chaque alerte les avis qui l'ont provoquée.

        POURQUOI ICI ET NON DANS `rules`. Les règles sont PURES — elles
        reçoivent des lignes déjà calculées et n'ouvrent aucune connexion.
        C'est ce qui les rend testables sans base et ce qui garde la décision
        de déclenchement au même endroit que sa justification. Aller chercher
        des verbatims est une lecture ; elle appartient au manager, qui a déjà
        le dépôt.

        Ne lève jamais : une notification sans extrait reste utile, une
        collecte qui échoue à cause d'un extrait manquant ne l'est pas.
        """
        if self.stats_repo is None or not alerts:
            return

        # La règle ne rend que le LIBELLÉ de la filiale ; le périmètre, lui, se
        # désigne par identifiant. On rapproche les deux via les lignes de
        # mouvement, qui portent les deux.
        par_label = {
            str(m.get("label")): m.get("key")
            for m in mouvements
            if m.get("label") is not None
        }

        for alert in alerts:
            sub_id = par_label.get(str(alert.company))
            if sub_id is None:
                continue
            try:
                scope = StatsFilter(
                    days=self.cfg.spike_window_days, subsidiaries=(int(sub_id),)
                )
                data = self.stats_repo.verbatims(
                    scope, polarity="negative", limit=_MAX_EXTRAITS
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Extraits indisponibles pour %s : %s", alert.company, e)
                continue

            for avis in data.get("reviews", []):
                texte = " ".join((avis.get("text") or "").split())
                if not texte:
                    continue
                if len(texte) > _EXTRAIT_CHARS:
                    texte = texte[:_EXTRAIT_CHARS].rstrip() + "…"
                # La DATE EN TÊTE, avant le texte. C'est elle qui prouve que
                # l'avis appartient bien à la fenêtre du pic ; la reléguer en
                # fin de ligne la ferait disparaître derrière une citation
                # tronquée.
                quand = _jour_court(avis.get("occurred_at"))
                source = avis.get("source")
                signature = " — ".join(x for x in (source,) if x)
                alert.evidence.append(
                    f"{quand} · {texte}" + (f" — {signature}" if signature else "")
                )

            self._attacher_evenements(alert, int(sub_id))

    def _attacher_evenements(self, alert: Alert, subsidiary_id: int) -> None:
        """Joint les événements extérieurs datés de la fenêtre du pic.

        POURQUOI UNE FENÊTRE PLUS LARGE QUE CELLE DU PIC. Une cause précède son
        effet, et le délai n'est pas nul : une hausse tarifaire annoncée le 3
        produit des avis mécontents le 10. Chercher la presse sur les seuls
        sept jours du pic écarterait mécaniquement l'article qui l'explique.
        Vers l'aval en revanche la fenêtre ne bouge pas — un article publié
        après les avis ne peut pas les avoir causés.

        Le dépôt élargit lui-même de la filiale au PAYS quand la filiale n'a
        pas de presse propre, et rend le périmètre retenu : « article
        national » et « article sur cette filiale » ne se valent pas, et la
        notification doit le dire.
        """
        try:
            from reviews.storage.press_repository import PressRepository

            fin = date.today() + timedelta(days=1)
            debut = fin - timedelta(
                days=self.cfg.spike_window_days + _AMORCE_PRESSE_JOURS
            )
            # LIMITE ÉLARGIE PAR RAPPORT À `_MAX_EVENEMENTS` : le dépôt trie le
            # négatif en tête mais rend aussi du neutre (une décision de
            # régulateur sort neutre du lexique — voir `PressRepository`). Sans
            # marge, un seul résultat neutre nous priverait de chercher plus
            # loin un négatif réellement disponible plus bas dans le classement.
            preuves = PressRepository(self.stats_repo.db).evidence(
                window=(debut, fin),
                level="subsidiary",
                value=str(subsidiary_id),
                limit=max(_MAX_EVENEMENTS, 5),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Événements indisponibles pour %s : %s", alert.company, e)
            return

        # ICI, ET NON DANS `PressRepository`, LE NEUTRE EST ÉCARTÉ.
        #
        # Le dépôt sert deux usages différents. Le diagnostic du modèle (voir
        # `llm/insights.py`) reçoit les articles neutres à dessein : une
        # décision de régulateur ou une hausse tarifaire s'écrit sans affect et
        # le modèle est celui qui JUGE si elle explique le mouvement — c'est le
        # rôle même de ce diagnostic. Cette notification n'a pas de modèle pour
        # trancher : elle affiche l'article tel quel sous « pourrait
        # coïncider ». Un article neutre — la vente d'actions d'un dirigeant,
        # mesurée le 18 août sur Vodafone Égypte — s'y lit alors comme un lien
        # de cause qu'aucune lecture humaine ne validerait. Sans négatif
        # disponible, mieux vaut ne rien montrer qu'une coïncidence qui
        # décrédibilise l'alerte entière.
        negatifs = [a for a in (preuves.get("articles") or []) if a.get("tonalite") == "negative"]
        if not negatifs:
            return
        articles = negatifs[:_MAX_EVENEMENTS]

        # Le périmètre est rappelé UNE fois, dans l'en-tête de la section,
        # plutôt que répété sur chaque ligne : trois articles suivis chacun de
        # « (presse nationale) » feraient trois fois le même avertissement.
        alert.events_scope = preuves.get("perimetre") or None
        for art in articles:
            # `PressRepository` rend la date en ISO ; on la ramène au même
            # format court que les avis, sinon la notification mélange
            # « 8 août » et « 2026-08-08 » dans deux sections voisines.
            quand = _jour_court(_date_iso(art.get("date")))
            alert.events.append(f"{quand} · {art.get('titre', '')}")

    def _recently_alerted(self, alert: Alert) -> bool:
        """Une alerte identique a-t-elle déjà été émise récemment ?

        INDISPENSABLE : le pipeline tourne plusieurs fois par jour, et un pic
        dure plusieurs jours. Sans ce silence, la même filiale réapparaîtrait à
        chaque passage et le fil métier deviendrait aussi illisible que le fil
        technique — précisément le défaut qu'on corrige.
        """
        if self.alert_repo is None or not alert.company:
            return False
        try:
            return self.alert_repo.has_recent(
                alert_type=alert.type,
                company=alert.company,
                hours=self.cfg.spike_cooldown_hours,
            )
        except Exception as e:  # noqa: BLE001
            # En cas de doute on laisse passer : une alerte en double est
            # moins grave qu'une alerte manquée sur une filiale qui décroche.
            logger.warning("Contrôle anti-répétition indisponible : %s", e)
            return False
