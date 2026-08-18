"""
Agent 2 — assistant de campagne, exposé à l'application web.

CE QUE CES ROUTES AJOUTENT, ET CE QU'ELLES NE FONT PAS
    Elles n'ajoutent AUCUNE règle métier. L'agent existait déjà et tournait en
    ligne de commande et sur Telegram ; il lui manquait une porte d'entrée HTTP.
    Chaque route se contente d'appeler une méthode publique de `CampaignAgent`
    et d'en rendre le résultat.

    C'est la condition pour que les trois surfaces — CLI, Telegram, web — ne
    divergent jamais. Une règle réécrite ici finirait par proposer une campagne
    différente selon l'endroit d'où on la demande, sans que rien ne le signale.

POURQUOI POST SUR DES LECTURES APPARENTES
    Même raison que pour `/insights` : proposer une campagne, décliner des
    contenus ou réviser un texte consomment le quota d'un fournisseur gratuit.
    Un GET serait préchargé par le navigateur, rejoué au retour dans l'onglet et
    revalidé par son cache — autant d'appels que personne n'a demandés. POST dit
    que l'appel est un ACTE.

    `/campaigns`, `/campaigns/{id}` et `/campaigns/{id}/dossier` restent des GET :
    ils ne lisent que la base.

LE PÉRIMÈTRE VIENT DES SÉLECTEURS, L'INTENTION DU TEXTE
    L'interface a des listes déroulantes : l'entité et la période y sont déjà
    validées. Les recomposer en phrase pour les faire re-deviner par un modèle
    coûterait un appel, risquerait un refus sur un périmètre pourtant certain, et
    pourrait rendre une AUTRE filiale que celle affichée. Le périmètre est donc
    construit ici, directement dans le contrat de filtre.

    Le texte libre continue de passer par le modèle, parce qu'il porte ce qu'un
    sélecteur ne dit pas : l'objectif visé, le canal souhaité, le ton, et les
    dimensions de segmentation que l'utilisateur croit disponibles — recueillies
    pour être RÉFUTÉES quand elles n'existent pas.

AUCUNE ROUTE NE REND 500 SUR UN ÉTAT PRÉVU
    Modèle absent, quota épuisé, période sans avis, campagne inconnue : ce sont
    des réponses, pas des pannes. Elles rendent 200 avec `available: false` et
    une raison en français, affichée telle quelle. Un 500 enverrait chercher un
    incident inexistant.
"""

import logging
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query

from reviews.agents.campagne import JOURS_DEFAUT
from reviews.api.deps import get_campaign_agent, get_campaign_dossier
from reviews.storage.filters import StatsFilter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

#: Statuts qu'un humain peut poser sur une proposition.
#:
#: Liste blanche : la valeur vient du corps de la requête et finit dans une
#: colonne contrainte. Une valeur libre y produirait soit une erreur SQL brute,
#: soit un statut que plus aucun écran ne sait afficher.
DECISIONS = ("validated", "rejected")


@router.get("/status")
def status(agent=Depends(get_campaign_agent)):
    """De quoi l'assistant est capable en ce moment.

    Interrogé par l'interface AVANT d'afficher le formulaire : proposer un champ
    de description libre qui échouera systématiquement — faute de clé d'API —
    est pire que de l'afficher désactivé avec sa raison.

    L'agent reste utilisable SANS modèle : les mesures, l'arbitrage et le
    gabarit de rédaction n'en dépendent pas. Seule la description libre en a
    besoin, d'où les deux drapeaux distincts.
    """
    client = agent.client
    raison = client.unavailable_reason() if client is not None else (
        "Aucun modèle n'est configuré."
    )
    return {
        "available": True,  # l'agent fonctionne toujours, gabarit compris
        "llm_available": raison is None,
        "llm_reason": raison,
        "description_libre": raison is None,
        "telegram": agent.notifier is not None,
        "jours_defaut": JOURS_DEFAUT,
    }


@router.get("")
def lister(
    limit: int = Query(20, ge=1, le=100),
    statut: Optional[str] = Query(
        None, description="Filtre sur le statut (proposed, validated, rejected)."
    ),
    agent=Depends(get_campaign_agent),
):
    """Les dernières propositions et leur statut."""
    try:
        lignes = agent.campagnes.lister(limit=limit, statut=statut)
    except Exception:  # noqa: BLE001
        logger.exception("Liste des campagnes indisponible")
        return {"available": False, "raison": "La liste n'a pas pu être lue.", "rows": []}
    return {"available": True, "rows": lignes}


@router.post("/propose")
def proposer(
    corps: Optional[dict] = Body(default=None),
    agent=Depends(get_campaign_agent),
):
    """Propose une campagne sur le périmètre demandé.

    Corps attendu (tous les champs facultatifs) :
        `subsidiary` / `operator` (entiers), `country` / `region` (chaînes),
        `days` (entier), `description` (texte libre), `dry_run` (booléen).

    SANS AUCUN CHAMP, l'agent choisit lui-même sa cible sur tout le périmètre
    suivi : c'est le mode du planificateur hebdomadaire, et il reste accessible
    ici pour qu'une démonstration puisse le montrer.
    """
    corps = corps or {}
    description = str(corps.get("description") or "").strip()
    dry_run = bool(corps.get("dry_run"))

    try:
        perimetre = _perimetre_des_selecteurs(corps)
    except ValueError as exc:
        # Un identifiant illisible est une erreur de l'appelant, pas une panne :
        # on le dit en clair plutôt que de mesurer un périmètre au hasard.
        return {"available": False, "raison": str(exc)}

    try:
        campagne = agent.proposer(
            description=description, dry_run=dry_run, perimetre=perimetre
        )
    except Exception:  # noqa: BLE001
        # `proposer` ne lève pas, mais l'API ne doit pas en DÉPENDRE : une
        # exception ici ferait tomber la page entière pour une fonctionnalité
        # accessoire.
        logger.exception("Proposition de campagne en échec")
        return {
            "available": False,
            "raison": "La proposition a échoué de mon côté. Réessayez dans un instant.",
        }

    if campagne.refus:
        # `ecartees` porte la raison PRÉCISE, cible par cible : « déjà proposée
        # il y a 4 jours », « segment trop petit », « pas assez d'avis ». Sans
        # elle, l'interface affiche un refus générique et l'utilisateur relance
        # indéfiniment la même demande sans comprendre ce qui bloque.
        return {
            "available": False,
            "raison": campagne.refus,
            "ecartees": campagne.ecartees,
        }

    return {
        "available": True,
        "campaign_id": campagne.campaign_id,
        "texte": campagne.texte(),
        "resume": campagne.resume(),
        "transmise": campagne.transmise,
        "redige_par_modele": campagne.redige_par_modele,
        "appels_llm": campagne.appels_llm,
        "dry_run": dry_run,
        **campagne.as_dict(),
    }


@router.get("/{campaign_id}")
def fiche(campaign_id: int, agent=Depends(get_campaign_agent)):
    """La fiche d'une campagne, en clair. Aucun appel de modèle."""
    return agent.fiche(campaign_id)


@router.get("/{campaign_id}/dossier")
def dossier(campaign_id: int, service=Depends(get_campaign_dossier)):
    """Le CAMPAIGN REPORT en treize sections — structuré ET en Markdown.

    `sections` sert l'affichage, `markdown` sert l'export et le copier-coller.
    Les deux viennent de la même composition : un rapport exporté ne peut pas
    différer de celui qui est à l'écran.

    Aucun appel de modèle : deux exécutions rendent le même dossier, et il reste
    produisible quand le fournisseur est indisponible.
    """
    return service.composer(campaign_id)


@router.post("/{campaign_id}/contenus")
def contenus(
    campaign_id: int,
    _corps: Optional[dict] = Body(default=None),
    agent=Depends(get_campaign_agent),
):
    """Décline le message en SMS, notification, e-mail, réseaux et annonce.

    UN SEUL appel de modèle pour les cinq formats. Déjà produits, ils sont
    rendus tels quels : régénérer donnerait un autre texte pour la même
    campagne, et l'équipe ne saurait plus lequel a été validé.
    """
    try:
        return agent.contenus(campaign_id)
    except Exception:  # noqa: BLE001
        logger.exception("Contenus indisponibles pour la campagne %s", campaign_id)
        return {
            "available": False,
            "raison": "Les contenus n'ont pas pu être produits. Réessayez dans un instant.",
        }


@router.post("/{campaign_id}/revoir")
def revoir(
    campaign_id: int,
    corps: Optional[dict] = Body(default=None),
    agent=Depends(get_campaign_agent),
):
    """Rejoue une campagne sous un autre ton ou un autre angle.

    Corps : `consigne` (texte libre) et/ou `strategie` (A, B ou C).

    UNE RÉVISION EST UNE NOUVELLE LIGNE, jamais un écrasement : sans quoi on ne
    pourrait plus comparer les deux versions, c'est-à-dire exercer le seul
    jugement pour lequel on a demandé une révision.
    """
    corps = corps or {}
    consigne = str(corps.get("consigne") or "").strip()
    strategie = str(corps.get("strategie") or "").strip() or None
    if not consigne and not strategie:
        return {
            "available": False,
            "raison": "Précisez une consigne (« plus rassurant ») ou une option (A, B ou C).",
        }

    try:
        campagne = agent.reviser(campaign_id, consigne, strategie)
    except Exception:  # noqa: BLE001
        logger.exception("Révision de la campagne %s en échec", campaign_id)
        return {"available": False, "raison": "La révision a échoué de mon côté."}

    if campagne.refus:
        return {"available": False, "raison": campagne.refus}
    return {
        "available": True,
        "campaign_id": campagne.campaign_id,
        "parent_id": campagne.parent_id,
        "texte": campagne.texte(),
        **campagne.as_dict(),
    }


@router.post("/{campaign_id}/decision")
def decider(
    campaign_id: int,
    corps: Optional[dict] = Body(default=None),
    agent=Depends(get_campaign_agent),
):
    """Valide ou rejette une proposition.

    AUCUNE CAMPAGNE NE PART SANS CETTE ÉTAPE. C'est la règle du dispositif :
    l'agent propose, un humain décide. Le statut est consigné avec son auteur,
    de sorte qu'une campagne validée reste rattachable à qui l'a validée.
    """
    corps = corps or {}
    statut = str(corps.get("statut") or "").strip()
    if statut not in DECISIONS:
        return {
            "available": False,
            "raison": f"Statut inconnu. Valeurs acceptées : {', '.join(DECISIONS)}.",
        }
    par = str(corps.get("par") or "").strip() or "interface web"

    try:
        applique = agent.campagnes.decider(campaign_id, statut, par)
    except Exception:  # noqa: BLE001
        logger.exception("Décision sur la campagne %s en échec", campaign_id)
        return {"available": False, "raison": "La décision n'a pas pu être enregistrée."}

    if not applique:
        return {
            "available": False,
            "raison": f"Campagne n°{campaign_id} inconnue ou déjà décidée.",
        }
    return {"available": True, "campaign_id": campaign_id, "statut": statut, "par": par}


@router.post("/{campaign_id}/telegram")
def telegram(
    campaign_id: int,
    _corps: Optional[dict] = Body(default=None),
    agent=Depends(get_campaign_agent),
):
    """Envoie la proposition à l'équipe marketing sur Telegram.

    CE N'EST PAS UNE ALERTE, et le message le dit dès son en-tête. Une alerte
    signale un incident et appelle une vérification ; une proposition de
    campagne appelle une décision. Les mélanger dans le même fil ferait perdre
    les deux : l'incident se noierait, la proposition passerait pour du bruit.

    Le message n'est JAMAIS composé ici : c'est celui de la campagne enregistrée,
    donc exactement ce que la fiche affiche. Un message rédigé pour Telegram
    seulement finirait par diverger de ce que l'équipe a validé à l'écran.
    """
    if agent.notifier is None:
        return {
            "available": False,
            "raison": "Aucun canal Telegram n'est configuré sur ce serveur.",
        }

    campagne = agent.campagnes.par_id(campaign_id)
    if campagne is None:
        return {"available": False, "raison": f"Campagne n°{campaign_id} inconnue."}

    try:
        envoye = agent._transmettre(agent._depuis_base(campagne))
    except Exception:  # noqa: BLE001
        logger.exception("Envoi Telegram de la campagne %s en échec", campaign_id)
        return {"available": False, "raison": "L'envoi Telegram a échoué."}

    if not envoye:
        return {
            "available": False,
            "raison": "Telegram n'a pas accepté le message. Voir les journaux du serveur.",
        }
    return {"available": True, "campaign_id": campaign_id, "transmise": True}


# ---------------------------------------------------------------------------
# Périmètre
# ---------------------------------------------------------------------------


def _perimetre_des_selecteurs(corps: dict) -> Optional[tuple[StatsFilter, int]]:
    """Traduit les sélecteurs de l'interface en périmètre exécutable.

    NE TOUCHE QUE LE PÉRIMÈTRE. L'intention — objectif, canal, et les dimensions
    de segmentation à réfuter — reste extraite de la description par le modèle,
    dans l'agent. C'est la dissymétrie qui compte : le sélecteur sait exactement
    quelle filiale on regarde, le modèle sait seul lire « pour les jeunes » et
    permettre à l'agent de répondre que l'âge n'existe pas en base.

    Renvoie `None` quand AUCUN périmètre n'a été choisi : l'agent reprend alors
    son chemin habituel — traduction complète de la description, ou choix
    automatique de la cible si elle est vide. C'est ce qui garde un seul jeu de
    règles pour les trois surfaces.

    Raises:
        ValueError: identifiant illisible. Mieux vaut le dire que mesurer un
            périmètre approchant : des chiffres justes sur la mauvaise filiale
            sont indétectables à la lecture.
    """
    axes: dict[str, Any] = {}
    for champ, cle in (("subsidiary", "subsidiaries"), ("operator", "operators")):
        valeur = corps.get(champ)
        if valeur in (None, "", []):
            continue
        try:
            axes[cle] = (int(valeur),)
        except (TypeError, ValueError):
            raise ValueError(
                f"Identifiant « {valeur} » illisible pour {champ} : un entier est attendu."
            )
    for champ, cle in (("country", "countries"), ("region", "regions")):
        valeur = corps.get(champ)
        if valeur in (None, "", []):
            continue
        axes[cle] = (str(valeur),)

    jours = corps.get("days")
    try:
        jours = int(jours) if jours not in (None, "") else JOURS_DEFAUT
    except (TypeError, ValueError):
        raise ValueError(f"Période « {jours} » illisible : un nombre de jours est attendu.")
    if not 1 <= jours <= 3650:
        raise ValueError("La période doit être comprise entre 1 et 3650 jours.")

    if not axes and jours == JOURS_DEFAUT:
        # Ni entité ni fenêtre particulière : rien à imposer, l'agent choisit.
        return None

    return StatsFilter(days=jours, **axes), jours
