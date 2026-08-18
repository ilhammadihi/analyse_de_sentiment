"""Endpoints des alertes."""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from reviews.api.deps import get_alert_repo
from reviews.api.filter_params import FilterDep
from reviews.storage.repository import AlertRepository

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
def list_alerts(
    f: FilterDep,
    limit: int = Query(50, ge=1, le=200),
    severity: Optional[str] = Query(None, pattern="^(info|warning|error)$"),
    kind: Optional[str] = Query(
        None,
        pattern="^(business|technical)$",
        description="`business` : alertes de satisfaction client. `technical` : "
        "santé de la collecte. Absent : les deux.",
    ),
    max_age_days: Optional[int] = Query(
        None,
        ge=1,
        le=365,
        description="Âge maximal d'une alerte, en jours, compté depuis "
        "maintenant. INDÉPENDANT de la fenêtre d'analyse : celle-ci dit sur "
        "quelle période on analyse, celui-ci à partir de quand une alerte "
        "cesse de décrire le présent. Absent : aucune borne de fraîcheur.",
    ),
    repo: AlertRepository = Depends(get_alert_repo),
):
    """Alertes récentes, enrichies de la filiale, du pays et de l'opérateur.

    Accepte le MÊME contrat de filtre que les endpoints d'agrégats : le fil
    d'alertes du dashboard doit parler du même périmètre que les chiffres
    affichés à côté de lui. `source_kind` et `source` sont ignorés — une alerte
    n'est ni un avis client ni un article.

    Le paramètre `kind` sépare les deux natures d'alerte, et ce n'est pas
    cosmétique : sur les 216 alertes en base, 215 sont techniques
    (« scraper_zero », « high_duplicates »). Dans un fil unique, elles
    enterrent le seul signal métier qui s'y trouve.
    """
    return repo.list_recent(
        limit=limit, severity=severity, kind=kind, f=f, max_age_days=max_age_days
    )
