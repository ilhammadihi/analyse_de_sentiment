"""
Le diagnostic — POURQUOI les données manquent, avant de chercher à les remplacer.

LA RÈGLE QUE CE MODULE FAIT RESPECTER
    « Si une filiale possède 0 avis, il ne faut PAS simplement relancer les
    collecteurs. »

    Ce n'est pas une précaution de style : un zéro a cinq causes possibles, et
    quatre d'entre elles rendent la relance inutile ou nuisible.

MESURÉ SUR LE CORPUS RÉEL (17 août 2026), et c'est ce qui a dicté le module
    Trois filiales n'ont aucun avis client. Le réflexe — relancer la collecte —
    aurait été faux pour les trois :

        Comores Telecom    unités Google Maps en `success`, items_inserted = 0
                           -> le collecteur a cherché et n'a rien trouvé.
                           Relancer coûte une session de navigateur par
                           passage, indéfiniment, pour un résultat connu.
                           42 articles de presse existent pourtant : l'entité
                           est parfaitement reconnue. C'est le CAS B, puis D.

        MTN RDC            unités mixtes : certaines jamais exécutées
                           (last_success_at NULL), d'autres réussies à vide.
                           -> une partie du diagnostic n'est pas encore
                           disponible. Conclure « aucune donnée n'existe »
                           serait prématuré. C'est le CAS « jamais tenté ».

    Un agent qui aurait répondu « je cherche de nouvelles sources » aux trois
    aurait eu tort deux fois sur trois, et de façon invérifiable.

POURQUOI C'EST DÉTERMINISTE, ET POURQUOI ÇA LE RESTE
    Aucun modèle n'intervient ici. Le diagnostic commande l'ACTION — relancer,
    corriger un mapping, chercher ailleurs, ou attendre — et une action
    déclenchée sur une réponse non reproductible ne se défend pas. C'est la
    même règle qui régit `arbitrage.py` pour l'Agent 1 : le modèle rédige, il
    ne décide jamais.

L'ORDRE DES TESTS EST LA LOGIQUE, PAS UNE COMMODITÉ
    Chaque cas n'est atteint que si les précédents sont écartés. Inverser deux
    tests change le diagnostic : chercher une nouvelle source avant d'avoir
    vérifié le mapping proposerait d'aller collecter ailleurs une donnée qu'on
    a déjà et qu'on range mal.
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from reviews.agents.quality.couverture import CouvertureFiliale

logger = logging.getLogger(__name__)


class Cas(str, Enum):
    """Les six états possibles d'une filiale, du plus sain au plus problématique."""

    #: Assez d'avis, sur assez de sources. Rien à faire.
    COUVERT = "couvert"

    #: Des avis, mais trop peu pour que ses taux soient comparables.
    SOUS_COUVERT = "sous_couvert"

    #: CAS A — un collecteur attendu échoue systématiquement.
    #: ACTION : alerte technique. NE PAS chercher de nouvelle source.
    COLLECTEUR_EN_ECHEC = "collecteur_en_echec"

    #: Les sources attendues n'ont pas encore tourné pour cette filiale.
    #: ACTION : attendre. Ce cas n'existait pas dans l'énoncé ; la file
    #: `collection_jobs` le rend visible, et sans lui MTN RDC serait déclarée
    #: « sans source exploitable » alors que 863 unités Google Maps attendent
    #: encore leur tour.
    JAMAIS_TENTE = "jamais_tente"

    #: CAS C — la donnée existe probablement mais n'arrive pas jusqu'ici.
    #: ACTION : proposer une correction de mapping, AVEC PREUVES. Jamais
    #: l'appliquer soi-même.
    MAPPING_SUSPECT = "mapping_suspect"

    #: CAS B — les collecteurs tournent et ne rendent rien.
    #: ACTION : ne plus insister sur ces sources.
    SOURCE_VIDE = "source_vide"

    #: CAS D — tout a été correctement tenté, rien n'est disponible.
    #: ACTION : et seulement ici, chercher des sources externes.
    AUCUNE_SOURCE_EXPLOITABLE = "aucune_source_exploitable"

    #: Aucune source d'avis clients n'est déclarée pour cette filiale.
    #: ACTION : c'est un défaut de NOTRE configuration, pas de la collecte.
    RIEN_DE_DECLARE = "rien_de_declare"


#: Cas pour lesquels il est légitime de chercher des sources externes.
#:
#: LISTE VOLONTAIREMENT COURTE. C'est le garde-fou de la section 5 de l'énoncé :
#: la recherche de sources ne se déclenche qu'une fois les sources existantes
#: correctement exécutées et constatées vides. Y ajouter
#: `COLLECTEUR_EN_ECHEC` reviendrait à contourner une panne en collectant
#: ailleurs — on masquerait le défaut au lieu de le corriger.
CAS_ENRICHISSABLES: frozenset[Cas] = frozenset(
    {Cas.AUCUNE_SOURCE_EXPLOITABLE, Cas.SOURCE_VIDE, Cas.RIEN_DE_DECLARE}
)

#: Cas qui appellent une intervention humaine avant toute autre chose.
CAS_BLOQUANTS: frozenset[Cas] = frozenset(
    {Cas.COLLECTEUR_EN_ECHEC, Cas.MAPPING_SUSPECT}
)


@dataclass
class Diagnostic:
    """Verdict sur une filiale : le cas, sa raison, et ce qu'il faut en faire."""

    subsidiary_id: int
    subsidiary: str
    cas: Cas
    raison: str

    #: Ce qu'il faut faire, en français et à l'impératif. Rédigé ici et non par
    #: un modèle : une recommandation d'action doit être identique d'un passage
    #: à l'autre pour les mêmes faits, sinon elle n'est pas actionnable.
    recommandation: str = ""

    #: Faits sur lesquels le verdict s'appuie. Jamais vide : un diagnostic sans
    #: preuve est une opinion, et l'énoncé interdit d'en produire.
    preuves: list[dict[str, Any]] = field(default_factory=list)

    @property
    def enrichissable(self) -> bool:
        """Peut-on légitimement chercher des sources externes pour cette filiale ?"""
        return self.cas in CAS_ENRICHISSABLES

    @property
    def bloquant(self) -> bool:
        return self.cas in CAS_BLOQUANTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "subsidiary_id": self.subsidiary_id,
            "subsidiary": self.subsidiary,
            "cas": self.cas.value,
            "raison": self.raison,
            "recommandation": self.recommandation,
            "enrichissable": self.enrichissable,
            "bloquant": self.bloquant,
            "preuves": self.preuves,
        }


def diagnostiquer(
    couverture: CouvertureFiliale,
    *,
    min_reviews: int = 10,
    min_sources: int = 2,
    indices_mapping: Optional[list[dict[str, Any]]] = None,
) -> Diagnostic:
    """Établit le cas d'une filiale. Fonction PURE : ni base, ni horloge, ni réseau.

    Args:
        couverture: l'état mesuré de la filiale.
        min_reviews: en deçà, la filiale est sous-couverte.
        min_sources: sources actives attendues pour un signal diversifié.
        indices_mapping: soupçons remontés par `mapping.py` (nom de sous-cible
            incohérent, alias manquant). Passés en argument plutôt que
            recalculés ici : ce module doit rester testable sans base.

    L'ordre des tests est la logique du module — voir l'en-tête.
    """
    indices_mapping = indices_mapping or []
    dit = _Preuves(couverture)

    # --- 0. Rien n'est déclaré : le défaut est chez nous ---------------------
    #
    # Testé EN PREMIER, parce que tous les tests suivants raisonnent sur des
    # sources attendues. Sans déclaration, « aucune source ne rend rien » est
    # vrai mais vide de sens : on n'a rien demandé.
    if not couverture.sources_attendues:
        return Diagnostic(
            subsidiary_id=couverture.subsidiary_id,
            subsidiary=couverture.subsidiary,
            cas=Cas.RIEN_DE_DECLARE,
            raison=(
                "Aucune source d'avis clients n'est déclarée pour cette filiale "
                "dans config/operators.json."
            ),
            recommandation=(
                "Vérifier la configuration de la filiale avant toute conclusion "
                "sur sa couverture : le manque vient d'ici, pas de la collecte."
            ),
            preuves=dit.base(),
        )

    # --- 0 bis. Panne TOTALE : plus aucune donnée ne peut arriver ------------
    #
    # TESTÉ AVANT LE VOLUME, et c'est tout l'enjeu de ce bloc. Une filiale dont
    # tous les collecteurs sont morts garde ses avis anciens : ils restent
    # nombreux, complets et cohérents, et le score les note fidèlement. Le
    # corpus est pourtant FIGÉ, et les Agents 1 et 2 continueraient de
    # raisonner dessus comme s'il vivait encore.
    #
    # Le test porte sur la TOTALITÉ des sources attendues, jamais sur une
    # seule : une panne partielle laisse d'autres sources alimenter la filiale,
    # et elle est déjà pénalisée par la composante de fiabilité du score. En
    # faire un diagnostic bloquant plafonnerait à 30 % une filiale par ailleurs
    # correctement couverte — une fausse alerte pour un vrai détail.
    if couverture.sources_en_erreur and set(couverture.sources_en_erreur) == set(
        couverture.sources_attendues
    ):
        return Diagnostic(
            subsidiary_id=couverture.subsidiary_id,
            subsidiary=couverture.subsidiary,
            cas=Cas.COLLECTEUR_EN_ECHEC,
            raison=(
                f"Aucune des {len(couverture.sources_attendues)} source(s) "
                f"attendue(s) n'aboutit : {', '.join(couverture.sources_en_erreur)}. "
                f"Les {couverture.avis_clients} avis en base ne sont plus "
                "renouvelés."
            ),
            recommandation=(
                "Corriger les collecteurs avant toute autre action. Les données "
                "existantes restent lisibles mais ne sont plus alimentées : tout "
                "indicateur calculé dessus vieillit sans le dire."
            ),
            preuves=dit.base() + dit.erreurs(),
        )

    # --- 1. La filiale est-elle simplement couverte ? ------------------------
    #
    # Le cas nominal est testé tôt : il concerne 117 filiales sur 135, et les
    # faire traverser toute la chaîne de diagnostic serait du travail pour rien.
    if couverture.avis_clients >= min_reviews:
        actives = len(couverture.sources_actives)
        if actives >= min_sources:
            return Diagnostic(
                subsidiary_id=couverture.subsidiary_id,
                subsidiary=couverture.subsidiary,
                cas=Cas.COUVERT,
                raison=(
                    f"{couverture.avis_clients} avis clients sur "
                    f"{actives} source(s) active(s)."
                ),
                preuves=dit.base(),
            )
        # Assez d'avis mais une seule source : ce n'est pas un trou de
        # couverture, c'est une FRAGILITÉ. Le signal existe, mais il dépend
        # entièrement d'une plateforme — et 130 filiales sur 135 sont dans ce
        # cas avec Google Maps. On le dit sans en faire une anomalie.
        return Diagnostic(
            subsidiary_id=couverture.subsidiary_id,
            subsidiary=couverture.subsidiary,
            cas=Cas.SOUS_COUVERT,
            raison=(
                f"{couverture.avis_clients} avis clients, mais une seule source "
                f"active ({', '.join(couverture.sources_actives) or 'aucune'}) : "
                "le signal dépend entièrement d'une plateforme."
            ),
            recommandation=(
                "Diversifier les sources de cette filiale pour que ses taux ne "
                "reposent plus sur une seule plateforme."
            ),
            preuves=dit.base() + dit.sources(),
        )

    # À partir d'ici, la filiale a MOINS que le minimum d'avis.

    # --- 2. CAS A — un collecteur attendu est en panne -----------------------
    #
    # PRIORITAIRE SUR TOUT LE RESTE, et c'est l'exigence explicite de l'énoncé :
    # tant qu'un collecteur échoue, on ne sait tout simplement pas ce que la
    # source contient. Chercher ailleurs reviendrait à contourner la panne, donc
    # à la rendre permanente — et à s'en apercevoir des mois plus tard.
    if couverture.sources_en_erreur:
        en_erreur = couverture.sources_en_erreur
        return Diagnostic(
            subsidiary_id=couverture.subsidiary_id,
            subsidiary=couverture.subsidiary,
            cas=Cas.COLLECTEUR_EN_ECHEC,
            raison=(
                f"{len(en_erreur)} collecteur(s) attendu(s) n'ont jamais abouti "
                f"pour cette filiale : {', '.join(en_erreur)}."
            ),
            recommandation=(
                "Corriger le collecteur avant toute autre action. Tant qu'il "
                "échoue, on ignore ce que la source contient — chercher une "
                "source de remplacement masquerait la panne."
            ),
            preuves=dit.base() + dit.erreurs(),
        )

    # --- 3. La collecte n'a pas encore eu lieu -------------------------------
    #
    # Placé AVANT le mapping et la recherche de sources : un diagnostic posé sur
    # une collecte incomplète est un diagnostic faux. Mesuré sur MTN RDC —
    # plusieurs unités Google Maps sans `last_success_at`, sur une file qui en
    # compte 863 en attente.
    if couverture.sources_jamais_tentees:
        jamais = couverture.sources_jamais_tentees
        return Diagnostic(
            subsidiary_id=couverture.subsidiary_id,
            subsidiary=couverture.subsidiary,
            cas=Cas.JAMAIS_TENTE,
            raison=(
                f"{len(jamais)} source(s) attendue(s) n'ont pas encore été "
                f"exécutées pour cette filiale : {', '.join(jamais)}."
            ),
            recommandation=(
                "Attendre le prochain passage avant de conclure. Aucune "
                "recherche de source externe ne se justifie tant que les "
                "sources déclarées n'ont pas été interrogées."
            ),
            preuves=dit.base() + dit.attente(),
        )

    # --- 4. CAS C — la donnée existe, mais pas sous ce nom -------------------
    #
    # Testé AVANT la recherche de sources externes : si la donnée est déjà
    # collectée mais mal rangée, aller la chercher ailleurs ne corrige rien et
    # ajoute une source à maintenir pour rien.
    if indices_mapping:
        return Diagnostic(
            subsidiary_id=couverture.subsidiary_id,
            subsidiary=couverture.subsidiary,
            cas=Cas.MAPPING_SUSPECT,
            raison=(
                f"{len(indices_mapping)} indice(s) de rattachement incohérent "
                "pour cette filiale, alors qu'elle n'a presque aucun avis."
            ),
            recommandation=(
                "Vérifier le rattachement AVANT de chercher une source : la "
                "donnée est peut-être déjà collectée et rangée ailleurs. La "
                "correction est proposée avec ses preuves, jamais appliquée "
                "automatiquement."
            ),
            preuves=dit.base() + indices_mapping,
        )

    # --- 5. CAS B — les sources ont répondu, et elles sont vides -------------
    muettes = couverture.sources_muettes
    if muettes and couverture.avis_clients == 0:
        # Le détail qui change la recommandation : la presse parle-t-elle de
        # cette filiale ? Si oui, l'entité EXISTE et est reconnue par la chaîne
        # d'attribution — le vide est donc bien celui des plateformes d'avis, et
        # non un problème d'identité. C'est l'argument qui autorise à passer au
        # cas D en confiance plutôt que de soupçonner un mapping.
        presse = couverture.articles_presse
        cas = (
            Cas.AUCUNE_SOURCE_EXPLOITABLE
            if len(muettes) == len(couverture.sources_attendues)
            else Cas.SOURCE_VIDE
        )
        raison = (
            f"Les {len(muettes)} source(s) attendue(s) ont été interrogées avec "
            f"succès et n'ont retourné aucun avis : {', '.join(muettes)}."
        )
        if presse:
            raison += (
                f" La filiale est pourtant citée par {presse} article(s) de "
                "presse : l'entité est reconnue, ce sont les plateformes d'avis "
                "qui ne la couvrent pas."
            )
        return Diagnostic(
            subsidiary_id=couverture.subsidiary_id,
            subsidiary=couverture.subsidiary,
            cas=cas,
            raison=raison,
            recommandation=(
                "Ne pas relancer ces collecteurs : ils fonctionnent et la source "
                "est vide. Rechercher des sources externes non encore exploitées."
                if cas is Cas.AUCUNE_SOURCE_EXPLOITABLE
                else "Ne plus insister sur ces sources ; elles répondent et sont vides."
            ),
            preuves=dit.base() + dit.sources() + dit.presse(),
        )

    # --- 6. Peu d'avis, sans cause identifiée --------------------------------
    #
    # Le repli. Il DIT qu'il n'a pas trouvé de cause, au lieu d'en désigner une
    # au hasard — même principe que `diagnose`, qui répond « aucune cause ne
    # domine » plutôt que d'élire la première venue.
    return Diagnostic(
        subsidiary_id=couverture.subsidiary_id,
        subsidiary=couverture.subsidiary,
        cas=Cas.SOUS_COUVERT,
        raison=(
            f"{couverture.avis_clients} avis clients seulement, sans panne de "
            "collecteur ni indice de mauvais rattachement identifié."
        ),
        recommandation=(
            "Surveiller. Le volume est trop faible pour produire un indicateur "
            "fiable, mais aucune cause technique n'est en jeu."
        ),
        preuves=dit.base() + dit.sources(),
    )


# ---------------------------------------------------------------------------
# Composition des preuves
# ---------------------------------------------------------------------------


class _Preuves:
    """Fabrique les preuves d'un diagnostic, au format de traçabilité commun.

    Chaque preuve porte SON TYPE, et c'est ce qui la rend relisable : « 0 avis »
    et « unité en succès sans insertion » sont deux faits de nature différente,
    et c'est leur conjonction — pas l'un des deux — qui établit le cas B.
    """

    def __init__(self, couverture: CouvertureFiliale):
        self.c = couverture

    def base(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "mesure",
                "source": "base de données",
                "fait": "volume d'avis clients",
                "valeur": self.c.avis_clients,
                "recents": self.c.avis_recents,
                "date": (
                    self.c.derniere_collecte.isoformat()
                    if self.c.derniere_collecte
                    else None
                ),
            }
        ]

    def sources(self) -> list[dict[str, Any]]:
        out = []
        for code in self.c.sources_attendues:
            etat = self.c.sources[code]
            out.append(
                {
                    "type": "source_attendue",
                    "source": code,
                    "avis": etat.avis,
                    "unites_deja_reussies": etat.unites_deja_reussies,
                    "items_inserted": etat.items_inserted,
                    "date": (
                        etat.derniere_collecte.isoformat()
                        if etat.derniere_collecte
                        else None
                    ),
                }
            )
        return out

    def erreurs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "erreur_collecte",
                "source": code,
                "unites_jamais_reussies": self.c.sources[code].unites_jamais_reussies,
                "message": (self.c.sources[code].derniere_erreur or "")[:200],
            }
            for code in self.c.sources_en_erreur
        ]

    def attente(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "unite_en_attente",
                "source": code,
                "unites_attente": self.c.sources[code].unites_attente,
            }
            for code in self.c.sources_jamais_tentees
        ]

    def presse(self) -> list[dict[str, Any]]:
        """La presse comme preuve d'EXISTENCE, jamais comme avis client.

        Distinction essentielle : ces articles ne comptent dans aucun taux de
        satisfaction — la migration 002 l'interdit — mais ils prouvent que
        l'entité est connue et correctement reconnue. C'est précisément ce qui
        permet d'écarter l'hypothèse d'un problème d'identité.
        """
        if not self.c.articles_presse:
            return []
        return [
            {
                "type": "preuve_existence",
                "source": "presse",
                "fait": "la filiale est citée par la presse collectée",
                "valeur": self.c.articles_presse,
            }
        ]
