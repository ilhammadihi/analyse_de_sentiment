"""
Agent 3 — gardien de la qualité des données et de leur enrichissement.

CE QU'IL AJOUTE AUX AGENTS 1 ET 2
    Les deux premiers agents raisonnent SUR les données. Aucun ne se demande si
    ces données méritent qu'on raisonne dessus. C'est un angle mort mesurable :
    au 17 août 2026, trois filiales du périmètre n'ont aucun avis client et
    quinze en ont moins de dix — et l'Agent 1 pouvait parfaitement produire un
    briefing sur une filiale dont le taux repose sur quatre avis.

    Cet agent-ci répond à la question préalable : « peut-on faire confiance à
    ce que dit cette filiale ? », et rend sa réponse aux deux autres sous la
    forme d'un DATA TRUST STATUS.

L'ORDRE DES ÉTAPES EST LA GARANTIE, comme pour l'Agent 1
    couverture -> DIAGNOSTIC -> mapping -> qualité -> score -> rédaction.

    Le diagnostic est en deuxième position et il est ENTIÈREMENT DÉTERMINISTE.
    C'est le cœur du dispositif : devant une filiale à zéro avis, il faut
    savoir si le collecteur est mort, si la source est vide, si le mapping est
    faux, ou si rien n'a encore été tenté — et ces quatre cas appellent quatre
    actions incompatibles. Les confondre, c'est relancer un collecteur qui
    fonctionne déjà (mesuré sur Comores Telecom : unités en `success`,
    `items_inserted = 0`) ou chercher une nouvelle source alors que le mapping
    est simplement à corriger.

    Le modèle n'entre qu'après, sur des cas déjà triés. Il ne décide jamais
    d'un diagnostic.

CE QU'IL NE FAIT PAS, ET NE PEUT PAS FAIRE
    Il ne collecte rien, ne modifie aucun avis, n'écrit dans aucune table
    d'avis, ne corrige aucun mapping de lui-même et ne supprime jamais rien.
    Il n'écrit que dans ses propres tables (migration 022). Une panne de cet
    agent ne peut donc pas abîmer les données — même invariant que l'Agent 1.
"""

from reviews.agents.quality.couverture import (
    CouvertureFiliale,
    MoniteurCouverture,
    SOURCES_ATTENDUES,
)
from reviews.agents.quality.diagnostic import Cas, Diagnostic, diagnostiquer
from reviews.agents.quality.score import ScoreQualite, calculer_score, statut_confiance

__all__ = [
    "CouvertureFiliale",
    "MoniteurCouverture",
    "SOURCES_ATTENDUES",
    "Cas",
    "Diagnostic",
    "diagnostiquer",
    "ScoreQualite",
    "calculer_score",
    "statut_confiance",
]
