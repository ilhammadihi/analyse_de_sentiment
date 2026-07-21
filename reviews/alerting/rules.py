"""
Règles d'alerte (pures : PipelineRun -> list[Alert], aucune I/O).

Ces règles sont volontairement orientées « signaux métier » : ce sont
exactement les déclencheurs que les futurs agents IA marketing exploiteront
(pic d'insatisfaction, chute de volume, etc.).
"""

from collections import Counter

from reviews.config import AlertingConfig
from reviews.domain.models import PipelineRun, Alert, AlertSeverity


def evaluate(run: PipelineRun, cfg: AlertingConfig) -> list[Alert]:
    """Évalue toutes les règles et retourne les alertes déclenchées."""
    if not cfg.enabled:
        return []

    alerts: list[Alert] = []
    alerts += _run_level_rules(run, cfg)
    alerts += _negative_spike_rules(run, cfg)
    alerts += _scraper_level_rules(run)
    return alerts


def _run_level_rules(run: PipelineRun, cfg: AlertingConfig) -> list[Alert]:
    alerts: list[Alert] = []

    if run.status == "failed":
        alerts.append(Alert(
            type="run_failed", severity=AlertSeverity.ERROR, run_id=run.run_id,
            title="Run échoué",
            message=f"Le run {run.run_id} a échoué : {run.error_message}",
        ))

    if cfg.alert_zero_reviews and run.total_reviews == 0:
        alerts.append(Alert(
            type="zero_reviews", severity=AlertSeverity.ERROR, run_id=run.run_id,
            title="Aucun avis collecté",
            message=f"Le run {run.run_id} n'a inséré aucun avis",
        ))

    if run.total_reviews > 0 and run.total_duplicates > run.total_reviews * 0.5:
        pct = run.total_duplicates / run.total_reviews * 100
        alerts.append(Alert(
            type="high_duplicates", severity=AlertSeverity.WARNING, run_id=run.run_id,
            title="Taux de doublons élevé",
            message=f"{run.total_duplicates}/{run.total_reviews} doublons ({pct:.0f}%)",
        ))

    if run.total_errors > 0:
        alerts.append(Alert(
            type="collect_errors", severity=AlertSeverity.WARNING, run_id=run.run_id,
            title="Erreurs de collecte",
            message=f"{run.total_errors} erreurs pendant la collecte",
        ))

    if run.duration_seconds and run.duration_seconds > 3600:
        alerts.append(Alert(
            type="slow_run", severity=AlertSeverity.WARNING, run_id=run.run_id,
            title="Durée d'exécution anormale",
            message=f"Le run a pris {run.duration_seconds / 60:.1f} minutes",
        ))

    return alerts


def _negative_spike_rules(run: PipelineRun, cfg: AlertingConfig) -> list[Alert]:
    """Pic de sentiment négatif par entreprise (déclencheur marketing)."""
    alerts: list[Alert] = []
    per_company: dict[str, Counter] = {}

    for result in run.scraper_results.values():
        for review in result.reviews:
            counter = per_company.setdefault(review.company, Counter())
            counter[review.sentiment] += 1
            counter["total"] += 1

    for company, counter in per_company.items():
        total = counter["total"]
        if total < cfg.min_reviews_for_ratio:
            continue
        ratio = counter.get("negative", 0) / total
        if ratio >= cfg.negative_ratio_threshold:
            alerts.append(Alert(
                type="negative_spike", severity=AlertSeverity.ERROR, run_id=run.run_id,
                company=company,
                title=f"Pic de sentiment négatif — {company}",
                message=(f"{ratio * 100:.0f}% d'avis négatifs sur {total} avis "
                         f"pour {company} lors de ce run"),
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
