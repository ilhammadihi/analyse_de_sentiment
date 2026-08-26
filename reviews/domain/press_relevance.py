"""
Un article de presse parle-t-il vraiment de télécommunications ?

LE PROBLÈME QU'ON CORRIGE
    `rss_feed` interroge Google News avec « <opérateur> <mot-clé> » puis
    attribue TOUS les articles renvoyés à cet opérateur, sans vérification.
    Google News étant un moteur flou, cela fait entrer :

      - « Attaque au Niger : Macron dénonce un attentat — Orange Actualités »
        (« Orange » n'est ici que le nom du média)
      - « Calendrier Ligue 1 Mobilis, CAN Féminine 2026 » (parrainage sportif)
      - « Le sarcophage de Toutânkhamon en restauration » (rien du tout)

    Mesuré sur un échantillon : environ un tiers du corpus de presse.

POURQUOI LA RÈGLE PORTE SUR LE VOCABULAIRE, PAS SUR LE NOM DE L'OPÉRATEUR
    Exiger que l'opérateur soit nommé paraît plus sûr, et c'est un piège. Le
    flux RSS ne fournit qu'un titre et deux lignes de résumé : un article
    parfaitement pertinent —

        « Internet mobile au Bénin : colère après la fin des forfaits
          illimités à 5 000 et 10 000 FCFA »

    — ne nomme aucun opérateur dans cet extrait, alors qu'il décrit exactement
    l'événement qui fait chuter la satisfaction. La règle du nom le rejetterait.

    Le vocabulaire métier, lui, sépare proprement les deux populations : les
    faux positifs observés parlent de football, de politique ou d'archéologie
    et n'emploient JAMAIS ce lexique, tandis qu'un article télécom l'emploie
    forcément — c'est son sujet.

CE QUE LA RÈGLE NE FAIT PAS
    Elle ne vérifie pas que l'article concerne LA BONNE filiale. Un article sur
    MTN Nigeria rattaché à MTN Ghana passerait. C'est un second problème, à
    traiter par la désambiguïsation géographique déjà écrite dans
    `press_feed.py` — pas ici.
"""

import re
import unicodedata

#: Version de la règle. À incrémenter dès que le lexique ou la logique change :
#: elle permet de savoir quels articles ont été jugés par quelle version, donc
#: de rejouer un tri sans redouter de mélanger deux verdicts incomparables.
RELEVANCE_VERSION = 1

#: Lexique métier, multilingue.
#:
#: Volontairement SPÉCIFIQUE. « service », « prix », « client » ou « société »
#: décriraient aussi bien un article sur une compagnie aérienne : les inclure
#: ferait rentrer le bruit qu'on cherche à sortir. Chaque terme retenu est
#: propre au secteur ou à ses pannes.
_TERMES = {
    # Français
    "reseau", "reseaux", "forfait", "forfaits", "internet", "donnees", "mobile",
    "telecom", "telecoms", "telecommunication", "telecommunications", "operateur",
    "operateurs", "abonne", "abonnes", "abonnement", "panne", "pannes", "coupure",
    "coupures", "recharge", "sms", "fibre", "itinerance", "debit", "couverture",
    "connexion", "gsm", "adsl", "smartphone", "puce", "portabilite", "regulateur",
    "bande passante", "haut debit", "tres haut debit", "communication",
    # Anglais
    "network", "networks", "data", "subscriber", "subscribers", "operator",
    "telco", "outage", "outages", "downtime", "tariff", "roaming", "coverage",
    "broadband", "fiber", "prepaid", "postpaid", "bundle", "airtime", "spectrum",
    "connectivity", "handset", "mobile money",
    # Portugais
    "rede", "redes", "dados", "movel", "operadora", "avaria", "tarifa",
    "cobertura", "chamada", "telemovel", "banda larga", "fibra", "recarga",
    # Générations mobiles, communes à toutes les langues
    "2g", "3g", "4g", "5g", "lte", "sim", "esim", "wifi", "wi-fi",
    # Vocabulaire d'ENTREPRISE du secteur.
    #
    # Ajouté après mesure : la première version ne couvrait que le vocabulaire
    # réseau et écartait la cession d'Orange Madagascar, le directeur de
    # l'ARCEP-Togo, le partenariat MTN-Ericsson, la dette de CAMTEL — des
    # articles parfaitement pertinents. Un opérateur ne fait pas que des
    # pannes : il rachète, se fait réguler, publie des résultats.
    "arcep", "artci", "regulateur", "regulation", "licence", "licences",
    "frequence", "frequences", "spectre", "concession", "operateur historique",
    "mobile money", "money", "monnaie electronique", "transfert d argent",
    "fintech", "portefeuille electronique", "wallet",
    # Équipementiers : leur présence signale une actualité d'infrastructure.
    "ericsson", "huawei", "nokia", "zte",
}

#: Termes arabes, laissés hors normalisation : retirer les diacritiques d'un
#: mot arabe ne le rapproche pas d'une forme canonique comme en français, et
#: `unicodedata` y produirait des résultats inattendus.
_TERMES_AR = {
    "شبكة",      # réseau
    "انترنت",    # internet
    "إنترنت",
    "اتصالات",   # télécommunications
    "باقة",      # forfait
    "رصيد",      # crédit
    "تغطية",     # couverture
    "عطل",       # panne
    "مشترك",     # abonné
}

_NON_MOT = re.compile(r"[^a-z0-9]+")


def _normalize(texte: str) -> str:
    """Minuscules, sans accents, ponctuation réduite à des espaces."""
    sans_accents = "".join(
        c for c in unicodedata.normalize("NFKD", texte or "")
        if not unicodedata.combining(c)
    )
    return f" {_NON_MOT.sub(' ', sans_accents.lower()).strip()} "


def termes_trouves(titre: str, texte: str) -> list[str]:
    """Termes métier repérés dans l'article. Sert autant au tri qu'à l'audit :
    pouvoir dire POURQUOI un article a été retenu est ce qui rend le filtre
    contestable, donc corrigeable."""
    brut = f"{titre or ''} {texte or ''}"
    normalise = _normalize(brut)

    trouves = [t for t in _TERMES if f" {t} " in normalise]
    trouves += [t for t in _TERMES_AR if t in brut]
    return sorted(trouves)


def est_pertinent(titre: str, texte: str) -> bool:
    """Vrai si l'article relève du secteur des télécommunications."""
    return bool(termes_trouves(titre, texte))
