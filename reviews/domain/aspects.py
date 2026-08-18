"""
Taxonomie des aspects métier d'un avis télécom.

POURQUOI UNE TAXONOMIE FERMÉE, ET NON DES MOTS-CLÉS LIBRES
    L'onglet « Motifs » repose aujourd'hui sur les termes du lexique qui se sont
    déclenchés. Mesuré sur 90 jours, il place en tête des motifs
    d'insatisfaction : « can't », « bad », « useless », « doesn't », « don't »,
    « its », « without », « ever ». Ce sont des mots-outils. Ils sont
    statistiquement liés au mécontentement, mais ils ne nomment rien : un
    responsable de filiale qui lit « can't » n'a aucune action à en tirer.

    Le défaut est de nature, pas de réglage. Un sac de mots ne peut extraire que
    des mots ; il ne rattachera jamais « le réseau tombe tous les soirs depuis la
    mise à jour » au motif « coupures réseau », faute d'un terme de lexique dans
    la phrase. Il faut une couche qui lise la phrase.

    Une extraction libre de « thèmes » par un modèle rendrait le problème
    inverse : autant de formulations que d'avis (« problème de connexion »,
    « connexion impossible », « can't connect »…), donc rien d'agrégeable. Une
    taxonomie FERMÉE force le modèle à choisir dans une liste, et c'est ce qui
    rend les motifs comptables, comparables d'une filiale à l'autre, et stables
    dans le temps.

CE QUE LA LISTE DOIT RESPECTER POUR RESTER UTILE
    - Chaque aspect désigne quelque chose sur quoi un opérateur peut AGIR. « Prix
      trop élevé » est un motif ; « mauvaise expérience » n'en est pas un.
    - Les aspects ne se recouvrent pas. Deux entrées voisines produisent des
      classements arbitraires, donc des séries temporelles qui oscillent sans
      qu'il se passe rien.
    - Leur nombre reste petit. Au-delà d'une vingtaine, chaque aspect porte trop
      peu d'avis pour qu'une variation soit significative sur une filiale.

TOUT CHANGEMENT DE CETTE LISTE IMPOSE D'INCRÉMENTER ``ASPECT_VERSION``.
Sans quoi un graphique comparerait des aspects issus de deux nomenclatures.
"""

from typing import Optional

#: Version de la taxonomie ET du prompt qui l'applique.
#:
#: Persistée sur chaque avis (`reviews.aspect_version`, migration 005). C'est ce
#: qui permet de savoir quelles lignes rejouer après une évolution : sans ce
#: numéro, on ré-analyserait tout le corpus à chaque déploiement — coûteux ici,
#: puisque chaque ligne consomme du quota d'un fournisseur.
#:
#:   v1 : taxonomie initiale, 16 aspects + « autre ».
ASPECT_VERSION = 1

#: Aspect de repli. Un avis compréhensible mais hors périmètre (« merci »,
#: « ok », une insulte) doit atterrir quelque part : sans cette case, le modèle
#: force un aspect au hasard et pollue un motif réel.
OTHER = "autre"


#: La taxonomie. Clé technique -> (libellé affiché, définition envoyée au modèle).
#:
#: Les définitions ne sont pas des commentaires : elles sont RECOPIÉES DANS LE
#: PROMPT. C'est le seul endroit où la frontière entre deux aspects est écrite,
#: et donc le seul levier pour corriger une confusion de classement.
ASPECTS: dict[str, tuple[str, str]] = {
    "reseau_couverture": (
        "Réseau & couverture",
        "qualité du signal, absence de réseau, zones non couvertes, 3G/4G/5G "
        "indisponible. NE PAS confondre avec la lenteur : ici il n'y a pas de "
        "réseau du tout, ou il est trop faible.",
    ),
    "debit_lenteur": (
        "Débit & lenteur",
        "connexion présente mais lente, téléchargement interminable, vidéo qui "
        "se met en mémoire tampon.",
    ),
    "coupures_pannes": (
        "Coupures & pannes",
        "interruptions de service, panne générale, service indisponible pendant "
        "des heures ou des jours.",
    ),
    "facturation_prix": (
        "Facturation & prix",
        "tarifs jugés trop élevés, facture incorrecte, crédit débité sans "
        "raison, prélèvement contesté.",
    ),
    "forfaits_data": (
        "Forfaits & données",
        "volume de données consommé trop vite, forfait qui expire, bundle mal "
        "décompté, conditions du forfait.",
    ),
    "recharge_paiement": (
        "Recharge & paiement",
        "échec d'une recharge, mobile money, transfert d'argent, paiement "
        "refusé, argent non crédité.",
    ),
    "service_client": (
        "Service client",
        "assistance injoignable, réclamation sans réponse, promesse non tenue, "
        "hotline. Concerne le SUIVI, à distance.",
    ),
    "agence_boutique": (
        "Agence & boutique",
        "accueil physique, temps d'attente au guichet, comportement du "
        "personnel en agence, horaires.",
    ),
    "app_bugs": (
        "Bugs de l'application",
        "l'application plante, se ferme, se bloque, affiche une erreur, ne se "
        "met pas à jour.",
    ),
    "app_connexion": (
        "Connexion & compte",
        "impossible de se connecter à l'application, code OTP non reçu, mot de "
        "passe, création ou vérification de compte.",
    ),
    "app_ergonomie": (
        "Ergonomie de l'application",
        "interface confuse, navigation pénible, publicités intrusives, "
        "fonctionnalité retirée, refonte mal accueillie.",
    ),
    "sim_identification": (
        "SIM & identification",
        "activation de la carte SIM, enregistrement d'identité, portabilité du "
        "numéro, SIM bloquée ou désactivée.",
    ),
    "roaming_international": (
        "Roaming & international",
        "utilisation à l'étranger, appels internationaux, frais de roaming.",
    ),
    "promotions_offres": (
        "Promotions & offres",
        "bonus, promotions, programme de fidélité, offre publicitaire trompeuse.",
    ),
    "fibre_domicile": (
        "Fibre & internet fixe",
        "internet à domicile, fibre, box, installation ou raccordement.",
    ),
    "fraude_securite": (
        "Fraude & sécurité",
        "arnaque, spam, appels frauduleux, souscription non consentie, données "
        "personnelles.",
    ),
    OTHER: (
        "Autre",
        "aucun aspect ci-dessus ne s'applique, ou l'avis n'exprime rien "
        "d'exploitable (« ok », « merci », insulte sans motif).",
    ),
}

#: Clés valides, pour rejeter ce que le modèle inventerait.
VALID_ASPECTS = frozenset(ASPECTS)


# ---------------------------------------------------------------------------
# DÉCOUPAGE APP / OPÉRATEUR
#
# Le corpus mélange deux jugements que rien ne distinguait : celui porté sur
# l'APPLICATION MOBILE et celui porté sur le SERVICE de la filiale. Les
# boutiques d'applications pèsent 83 % des avis clients, et leurs trois premiers
# motifs négatifs — app_bugs (3 598 avis), app_connexion (2 381), app_ergonomie
# (1 715) — décrivent un logiciel, pas un opérateur télécom.
#
# Confondus, ils produisent une faute en chaîne : une mise à jour ratée fait
# monter la part de négatifs d'une filiale, `negative_spike` tire un « pic de
# mécontentement », et l'Agent 1 en cherche la cause du côté du réseau ou de la
# facturation — qui n'ont pas bougé.
#
# La séparation ne pouvait PAS se faire par source : 4 114 avis de boutiques ne
# parlent que de l'opérateur (recharge 1 067, facturation 1 050, service client
# 1 010). Elle se fait donc avis par avis, sur les aspects que le modèle a
# reconnus — d'où ce découpage, qui est la seule chose qu'il faut déclarer.
#
# CE DÉCOUPAGE NE CHANGE PAS CE QUE LE MODÈLE PRODUIT : il ne rentre pas dans le
# prompt, il classe après coup. ``ASPECT_VERSION`` n'a donc pas à bouger, et
# aucune ligne n'est à ré-analyser.
#
# LE MIROIR SQL EST DANS ``dim_aspect`` (migration 019). C'est cette table que
# lisent les vues, parce que le SQL ne peut pas importer ce module. Les deux
# doivent rester d'accord : ``tests/test_review_about.py`` échoue sinon.
# ---------------------------------------------------------------------------

#: Aspects qui nomment un grief APPLICATIF — l'éditeur du logiciel est en cause,
#: pas l'opérateur. Un avis qui n'en contient aucun n'a rien à voir avec l'app.
APP_ASPECTS = frozenset({"app_bugs", "app_connexion", "app_ergonomie"})

#: Aspects qui nomment un grief de SERVICE. Défini par SOUSTRACTION, jamais
#: recopié : un aspect ajouté à la taxonomie et oublié ici tomberait sinon dans
#: aucune des deux listes, et ses avis seraient classés par présomption de
#: source au lieu de l'être sur ce qu'ils disent — silencieusement.
OPERATOR_ASPECTS = VALID_ASPECTS - APP_ASPECTS - {OTHER}


def scope(aspect: str) -> str:
    """De quel côté du découpage un aspect tombe : 'app', 'operator' ou 'none'.

    'none' est réservé au repli ``OTHER`` : il ne doit faire basculer un avis
    d'aucun côté, sans quoi la moitié du corpus — les « good », « smooth »,
    « très bien » — trancherait au hasard de ce que le modèle en a fait.
    """
    if aspect in APP_ASPECTS:
        return "app"
    if aspect in OPERATOR_ASPECTS:
        return "operator"
    return "none"

#: Nombre maximal d'aspects retenus par avis et par polarité.
#:
#: Un avis qui « coche » huit aspects n'a en réalité été compris par personne :
#: soit le modèle a saupoudré, soit le texte est un pavé qui mériterait d'être
#: lu à la main. Plafonner évite qu'un tel avis pèse huit fois dans les
#: classements de motifs, alors qu'un avis normal y pèse une ou deux fois.
MAX_ASPECTS_PER_POLARITY = 3


def label(aspect: str) -> str:
    """Libellé affichable d'un aspect ; la clé elle-même si elle est inconnue."""
    entry = ASPECTS.get(aspect)
    return entry[0] if entry else aspect


def normalize(raw: Optional[str]) -> Optional[str]:
    """Ramène une valeur produite par le modèle à une clé de la taxonomie.

    Renvoie ``None`` si la valeur n'appartient pas à la taxonomie. C'est
    volontairement STRICT : accepter un aspect inventé revient à rouvrir la
    taxonomie, donc à retrouver le nuage de formulations non agrégeables que
    cette liste fermée existe pour éviter. Un rejet est journalisé et l'avis
    reste simplement sans cet aspect.
    """
    if not raw:
        return None
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return key if key in VALID_ASPECTS else None


def taxonomy_for_prompt() -> str:
    """La taxonomie, mise en forme pour être insérée dans le prompt.

    Générée depuis ``ASPECTS`` plutôt que recopiée dans le prompt : une liste
    tenue à deux endroits diverge, et le modèle se mettrait alors à produire des
    aspects que ``normalize()`` rejette — une perte silencieuse.
    """
    return "\n".join(
        f"- {key} : {definition}" for key, (_, definition) in ASPECTS.items()
    )
