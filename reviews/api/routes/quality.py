"""
Endpoints de l'Agent 3 — l'espace « Data Quality » du dashboard.

POURQUOI `/quality/trust` EST L'ENDPOINT LE PLUS IMPORTANT
    Les autres routes servent un écran. Celle-ci sert les AUTRES AGENTS : elle
    rend le DATA TRUST STATUS, c'est-à-dire la réponse à « peut-on se fier à ce
    que dit cette filiale ? ». C'est le seul contrat de ce module qui, s'il
    change, casse quelque chose ailleurs.

TOUTES LES LECTURES SONT EN GET, CONTRAIREMENT À `/insights/*`
    Les synthèses sont en POST parce qu'un appel consomme le quota d'un
    fournisseur gratuit et qu'un GET serait préchargé et rejoué. Ici, rien
    n'appelle de modèle : ce sont des lectures de tables, idempotentes et sans
    coût. Le POST est réservé à `/quality/run`, qui est le seul ACTE.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from reviews.api.deps import get_quality_agent, get_quality_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/quality", tags=["quality"])


@router.get("/overview")
def overview(depot=Depends(get_quality_repo)):
    """Vue d'ensemble : score global, répartition des statuts, files en attente.

    C'est la tuile d'accueil de l'onglet. Les compteurs qu'elle rend — constats
    à instruire, candidates, affirmations non corroborées — sont exactement les
    trois files sur lesquelles un humain peut agir.
    """
    return depot.resume()


@router.get("/subsidiaries")
def subsidiaries(
    status: Optional[str] = Query(
        None,
        pattern="^(TRUSTED|ACCEPTABLE|DEGRADED|UNTRUSTED)$",
        description="Filtre sur le statut de confiance.",
    ),
    depot=Depends(get_quality_repo),
):
    """Score de qualité par filiale, la plus faible d'abord.

    Triée par score croissant et non alphabétiquement : un écran de qualité
    existe pour montrer ce qui ne va pas, et l'ordre alphabétique enterrerait
    les filiales en défaut au milieu des autres.
    """
    lignes = depot.trust()
    if status:
        lignes = [l for l in lignes if l["status"] == status]
    return {"total": len(lignes), "subsidiaries": lignes}


@router.get("/trust")
def trust(
    subsidiary_id: Optional[int] = Query(
        None, description="Filiale précise. Absent = toutes."
    ),
    depot=Depends(get_quality_repo),
):
    """DATA TRUST STATUS — consommé par les Agents 1 et 2.

    RENVOIE TOUJOURS 200, y compris quand aucun instantané n'existe encore : la
    liste est alors vide. Un 404 ferait échouer l'appelant sur un état
    parfaitement normal — l'agent n'a simplement pas encore tourné — et le
    priverait de sa capacité à décider de se taire.

    L'ABSENCE D'INSTANTANÉ N'EST PAS UNE AUTORISATION. Un appelant qui ne
    trouve pas sa filiale ici doit traiter la donnée comme non vérifiée, jamais
    comme fiable par défaut.
    """
    lignes = depot.trust(subsidiary_id)
    return {
        "total": len(lignes),
        "trust": lignes,
        "note": (
            "Une filiale absente de cette liste n'a pas encore été évaluée : "
            "sa donnée doit être traitée comme non vérifiée."
        ),
    }


@router.get("/flags")
def flags(
    status: Optional[str] = Query(
        None, pattern="^(FLAGGED|REVIEW_REQUIRED|ACCEPTED|REJECTED)$"
    ),
    subsidiary_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    depot=Depends(get_quality_repo),
):
    """Constats de qualité, avec leurs preuves et qui les a produits.

    `detected_by` distingue les constats de RÈGLE de ceux du MODÈLE. C'est la
    colonne la plus utile de l'écran : un doublon établi par égalité de chaînes
    et un doublon présumé par un modèle n'appellent pas la même confiance.
    """
    return {"flags": depot.constats(status=status, subsidiary_id=subsidiary_id, limit=limit)}


@router.get("/candidates")
def candidates(
    subsidiary_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=500),
    depot=Depends(get_quality_repo),
):
    """Sources candidates, avec le résultat MESURÉ de leur sonde HTTP.

    `probe_status` et `accessibility` sont le cœur de la réponse : une
    candidate sans sonde est une piste, une candidate sondée est un fait. Le
    dashboard doit présenter les deux différemment.
    """
    return {"candidates": depot.candidates(subsidiary_id=subsidiary_id, limit=limit)}


@router.get("/claims")
def claims(
    subsidiary_id: Optional[int] = None,
    status: Optional[str] = Query(
        None, pattern="^(CONFIRMED|CORROBORATED|PLAUSIBLE|UNCONFIRMED)$"
    ),
    jours: int = Query(30, ge=1, le=365),
    depot=Depends(get_quality_repo),
):
    """Affirmations extraites des avis et leur degré de corroboration.

    L'écran doit afficher les `UNCONFIRMED` AUSSI VISIBLEMENT que les autres :
    c'est la liste de ce qu'il ne faut PAS relayer, et elle a autant de valeur
    opérationnelle que la liste de ce qui est établi.
    """
    return {
        "claims": depot.affirmations(
            subsidiary_id=subsidiary_id, status=status, jours=jours
        )
    }


@router.get("/orphans")
def orphans(
    status: Optional[str] = Query(
        None, pattern="^(AUTO_SAFE|HIGH_CONFIDENCE|REVIEW_REQUIRED|UNRESOLVED)$"
    ),
    limit: int = Query(100, ge=1, le=500),
    depot=Depends(get_quality_repo),
):
    """Avis sans filiale : compteurs, et propositions de réattribution.

    `resume.restants` compte les avis RÉELLEMENT encore orphelins dans
    `reviews` ; `par_statut` décrit les propositions non appliquées. Les deux
    sont nécessaires et ne disent pas la même chose : une proposition peut
    exister pour un avis déjà rattaché par un autre chemin.

    Aucune écriture ici. La réattribution passe par la CLI
    (`python -m reviews orphelins --appliquer`), délibérément : elle modifie
    `reviews`, la seule table que tout le reste de l'Agent 3 s'interdit de
    toucher, et cela ne doit pas être déclenchable par une requête web.
    """
    return {
        "resume": depot.orphelins_resume(),
        "propositions": depot.propositions(status=status, limit=limit),
    }


@router.post("/run")
def run(
    dry_run: bool = Query(
        True,
        description="Vrai par défaut : analyse sans rien écrire ni notifier. "
        "Passer à false déclenche un passage réel, qui peut écrire en base et "
        "envoyer une notification Telegram.",
    ),
    _body: Optional[dict] = Body(default=None),
    agent=Depends(get_quality_agent),
):
    """Déclenche un passage de l'Agent 3.

    `dry_run=true` PAR DÉFAUT, contrairement à la CLI. Une route HTTP est
    appelable par erreur — un rechargement, un outil de test, un lien partagé —
    et le défaut d'une action déclenchable par accident doit être celle qui ne
    fait rien d'irréversible. Un passage réel envoie une notification à toute
    l'équipe ; il doit être demandé explicitement.
    """
    try:
        passage = agent.run(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Passage de l'agent qualité en échec")
        raise HTTPException(
            status_code=500, detail=f"Passage en échec : {exc}"
        ) from exc
    return {"dry_run": dry_run, **passage.as_dict()}
