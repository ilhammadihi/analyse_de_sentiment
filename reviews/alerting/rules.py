"""
Règles d'alerte — pures, sans aucune I/O.

DEUX FAMILLES, à ne jamais mélanger dans un même fil :

  - TECHNIQUES (`_run_level_rules`, `_scraper_level_rules`) : la collecte
    fonctionne-t-elle ? Elles décrivent le pipeline, pas le marché.
  - MÉTIER (`negative_spike_alerts`) : la satisfaction se dégrade-t-elle ?
    C'est le seul signal qu'un responsable marketing doit lire, et le
    déclencheur des futurs agents de campagne.

    Les alertes techniques sont ~200 fois plus nombreuses : présentées
    ensemble, elles enterrent le signal métier. Le dashboard les sépare via
    `kind=business|technical` (voir BUSINESS_ALERT_TYPES).

CE QU'EST UN PIC, ET CE QU'IL N'EST PAS
    Un pic est une VARIATION sur une COURTE PÉRIODE. La règle précédente
    mesurait un NIVEAU — la part de négatifs parmi les avis d'un run — ce qui
    ne pouvait pas marcher : un run collecte des avis publiés de 1970 à
    aujourd'hui, son ratio ne décrit donc aucune période. Elle exigeait en
    outre 50 % de négatifs quand la moyenne du corpus est à 17 %. Résultat :
    une seule alerte métier sur 216.

    La détection s'appuie désormais sur les DATES DE PUBLICATION : une fenêtre
    courte comparée à la précédente, de durée égale. C'est le même calcul que
    le bloc « Ce qui a changé » du dashboard, ce qui garantit qu'une alerte
    correspond toujours à quelque chose de visible à l'écran.
"""

from typing import Any, Iterable

from reviews.config import AlertingConfig
from reviews.domain.models import PipelineRun, Alert, AlertSeverity


def evaluate(run: PipelineRun, cfg: AlertingConfig) -> list[Alert]:
    """Évalue les règles TECHNIQUES (celles qui ne dépendent que du run).

    Les alertes métier ne sont pas ici : elles exigent l'historique en base et
    sont produites par `negative_spike_alerts`, appelée par l'AlertManager.
    """
    if not cfg.enabled:
        return []

    alerts: list[Alert] = []
    alerts += _run_level_rules(run, cfg)
    alerts += _scraper_level_rules(run)
    return alerts


def negative_spike_alerts(
    degraded: Iterable[dict[str, Any]], cfg: AlertingConfig
) -> list[Alert]:
    """Transforme des variations mesurées en alertes métier.

    PURE : reçoit des lignes déjà calculées (par StatsRepository.movers) et ne
    fait qu'appliquer les seuils. C'est ce qui la rend testable sans base, et
    ce qui garde la décision au même endroit que sa justification.

    Args:
        degraded: lignes de variation, telles que renvoyées par `movers()` —
            `label`, `part_negatifs`, `part_negatifs_avant`, `delta_negatifs`,
            `avis_clients`, plus le contexte (`operator`, `country`).

    Deux motifs de déclenchement, volontairement distincts :
      - une HAUSSE d'au moins `spike_delta_points` : la filiale se dégrade,
        même si son niveau reste modéré ;
      - un NIVEAU absolu au-delà de `negative_ratio_threshold` : rattrape la
        filiale déjà très dégradée mais stable, qu'une règle de variation
        seule laisserait passer indéfiniment.
    """
    seuil_absolu = cfg.negative_ratio_threshold * 100
    alerts: list[Alert] = []

    for row in degraded:
        avis = row.get("avis_clients") or 0
        part = row.get("part_negatifs")
        delta = row.get("delta_negatifs") or 0.0
        if part is None or avis < cfg.min_reviews_for_ratio:
            continue

        part = float(part)
        monte = delta >= cfg.spike_delta_points
        eleve = part >= seuil_absolu
        if not (monte or eleve):
            continue

        # Gravité : une hausse deux fois supérieure au seuil, ou plus d'un avis
        # sur deux négatif, appelle une réaction immédiate. Le reste est un
        # avertissement — sans cette nuance, tout arrive au même niveau et
        # l'échelle de gravité ne sert plus à rien.
        grave = delta >= 2 * cfg.spike_delta_points or part >= 50
        contexte = " · ".join(
            str(x) for x in (row.get("operator"), row.get("country")) if x
        )

        if monte:
            avant = row.get("part_negatifs_avant")
            detail = (
                f"{float(avant):.0f} % → {part:.0f} % d'avis négatifs "
                f"(+{delta:.0f} points) sur {cfg.spike_window_days} jours, "
                f"{avis} avis clients"
            )
        else:
            detail = (
                f"{part:.0f} % d'avis négatifs sur {avis} avis clients "
                f"des {cfg.spike_window_days} derniers jours"
            )

        alerts.append(Alert(
            type="negative_spike",
            severity=AlertSeverity.ERROR if grave else AlertSeverity.WARNING,
            company=row.get("label"),
            title=f"Pic de mécontentement — {row.get('label')}",
            message=f"{detail}{f' [{contexte}]' if contexte else ''}",
        ))

    return alerts


def _run_level_rules(run: PipelineRun, cfg: AlertingConfig) -> list[Alert]:
    alerts: list[Alert] = []

    if run.status == "failed":
        alerts.append(Alert(
            type="run_failed", severity=AlertSeverity.ERROR, run_id=run.run_id,
            title="Run échoué",
            message=f"Le run {run.run_id} a échoué : {run.error_message}",
        ))

    # « Aucun avis » n'a de sens que pour un run MULTI-SOURCES : il dit alors
    # que la chaîne entière est muette. Depuis qu'un job planifie chaque source
    # séparément, un run n'en porte qu'une, et cette règle doublonnerait
    # `scraper_zero` — en moins précis, puisqu'elle ne nomme pas la source, et
    # en plus grave (error contre warning) alors qu'une source sans nouveauté
    # est le cas ordinaire. Laissée telle quelle, elle produisait une alerte
    # « error » par source et par cycle.
    multi_sources = len(run.scraper_results) != 1
    if cfg.alert_zero_reviews and run.total_reviews == 0 and multi_sources:
        alerts.append(Alert(
            type="zero_reviews", severity=AlertSeverity.ERROR, run_id=run.run_id,
            title="Aucun avis collecté",
            message=f"Le run {run.run_id} n'a inséré aucun avis",
        ))

    # Taux rapporté au VOLUME TRAITÉ (nouveaux + doublons), et non aux seuls
    # nouveaux. Diviser par les nouveaux produit un « taux » sans plafond : un
    # run à 12 nouveaux pour 417 doublons affichait « 3475 % », juste sous la
    # carte « Derniers passages » qui annonçait 97,2 % pour le même run — deux
    # chiffres contradictoires sur un même écran. Le dénominateur retenu ici est
    # celui de `taux_doublons` dans stats_repository.py : une seule définition.
    traites = run.total_reviews + run.total_duplicates
    if traites > 0 and run.total_duplicates > traites * 0.5:
        pct = run.total_duplicates / traites * 100
        alerts.append(Alert(
            type="high_duplicates", severity=AlertSeverity.WARNING, run_id=run.run_id,
            title="Taux de doublons élevé",
            message=f"{run.total_duplicates} doublons sur {traites} téléchargés ({pct:.1f} %)",
        ))

    if run.total_errors > 0:
        alerts.append(Alert(
            type="collect_errors", severity=AlertSeverity.WARNING, run_id=run.run_id,
            title="Erreurs de collecte",
            message=f"{run.total_errors} erreurs pendant la collecte",
        ))

    # « Trop lent » se mesure contre la CADENCE de la source, pas contre une
    # heure fixe. Google Maps demande une dizaine d'heures pour une cadence de
    # vingt-quatre : il est dans son budget, et le seuil absolu l'aurait signalé
    # en panne à chaque passage. Inversement, une source à six heures de cadence
    # qui en prend sept ne rattrapera jamais son retard — c'est précisément ce
    # qui a rendu l'alerting muet, et c'est ce que cette règle doit voir.
    budget = run.budget_seconds or 3600
    if run.duration_seconds and run.duration_seconds > budget:
        alerts.append(Alert(
            type="slow_run", severity=AlertSeverity.WARNING, run_id=run.run_id,
            title="Durée d'exécution anormale",
            message=(
                f"Le run a pris {run.duration_seconds / 60:.1f} minutes, "
                f"au-delà de son budget de {budget / 60:.0f} minutes"
            ),
        ))

    return alerts


def _scraper_level_rules(run: PipelineRun) -> list[Alert]:
    alerts: list[Alert] = []
    for name, result in run.scraper_results.items():
        if result.status == "failed":
            alerts.append(Alert(
                type="scraper_failed", severity=AlertSeverity.ERROR, run_id=run.run_id,
                source=name,
                title=f"Collecteur {name} en échec",
                message=result.error_message or "Erreur inconnue",
            ))
        elif result.status == "success" and result.inserted_count == 0:
            alerts.append(Alert(
                type="scraper_zero", severity=AlertSeverity.WARNING, run_id=run.run_id,
                source=name,
                title=f"{name} : zéro nouvel avis",
                message=f"Le collecteur {name} n'a inséré aucun nouvel avis",
            ))
    return alerts
