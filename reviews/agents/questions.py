"""
Contrat de question : ce qu'on peut demander à l'assistant conversationnel.

POURQUOI CE MODULE EXISTE — LE PIÈGE QU'IL FERME
    Un modèle sait écrire du SQL. Il le ferait, souvent bien, et un jour il
    inventerait une colonne, un nom de table ou un `WHERE` sur une dimension
    qui n'existe pas. Le symptôme ne serait pas une erreur : ce serait une
    réponse plausible et fausse, impossible à distinguer d'une bonne.

    Le modèle ne produit donc JAMAIS de requête. Il produit des PARAMÈTRES,
    qui sont validés ici contre le contrat de filtre déjà utilisé par tout le
    dashboard (`storage/filters.py`). Ce qui n'entre pas dans le contrat est
    refusé avec une phrase en français — jamais deviné, jamais approximé.

    Conséquence directe : la surface d'attaque du modèle est exactement celle
    d'une barre de filtres. Il peut se tromper de périmètre, il ne peut pas
    interroger autre chose que ce que le dashboard interroge déjà.

CE QUE VALIDER VEUT DIRE ICI
    1. Un nom d'entité est RÉSOLU contre le catalogue réel de la base, jamais
       recopié dans une requête. « Orange » devient `operator_id = 7`, et
       « Orangé » ne devient rien du tout.
    2. Tout seuil — fenêtre par défaut, nombre de lignes, plancher de volume —
       est décidé ICI, en Python. Le prompt n'en porte aucun (règle du projet :
       un seuil écrit dans un prompt est un seuil qu'on ne peut ni tester ni
       faire varier).
    3. Ce qui sort est un `StatsFilter` gelé, exécutable tel quel par les
       repositories existants.
"""

import unicodedata
from dataclasses import dataclass
from typing import Any, Optional

from reviews.storage.filters import LEVELS, StatsFilter

# ---------------------------------------------------------------------------
# Vocabulaire du contrat
# ---------------------------------------------------------------------------

#: Intentions servies, avec la glose montrée au modèle.
#:
#: UNE SEULE POUR L'INSTANT, ET C'EST DÉLIBÉRÉ. Mieux vaut cinq questions
#: parfaites qu'une promesse ouverte : un robot qui répond à tout répond
#: forcément mal à quelque chose, et c'est cette réponse-là dont on se
#: souviendra. Les intentions suivantes (motifs dominants, diagnostic « pourquoi
#: X se dégrade ») s'ajoutent ici, chacune avec son exécuteur.
INTENTIONS: dict[str, str] = {
    "classement": (
        "classer des entités sur une période — « qui revient le plus », "
        "« qui a le plus d'avis négatifs », « quelle filiale est la mieux notée »"
    ),
}

#: Niveaux d'agrégation ouverts aux questions, avec leur glose française.
#:
#: SOUS-ENSEMBLE VOLONTAIRE de `filters.LEVELS` : `source` y figure (« quelle
#: source rapporte le plus d'avis ») mais ne répond à aucune question métier
#: qu'un encadrant poserait, et l'ouvrir invite le modèle à y ranger les
#: questions qu'il n'a pas comprises. Les clés restent celles du contrat de
#: filtre — c'est `resolve_level` qui tranche en dernier ressort.
NIVEAUX: dict[str, str] = {
    "subsidiary": "une filiale (Orange Mali, MTN Ghana…)",
    "operator": "un opérateur, tous pays confondus (Orange, MTN…)",
    "country": "un pays",
    "region": "une région d'Afrique",
}

#: Tris ouverts aux questions, avec leur glose.
#:
#: Les clés DOIVENT exister dans `stats_repository._SORTS`, qui porte le SQL
#: correspondant. Un test verrouille cette correspondance : un tri retiré
#: là-bas et laissé ici ferait retomber silencieusement le classement sur son
#: tri par défaut, c'est-à-dire répondre à une autre question que celle posée.
TRIS: dict[str, str] = {
    "volume": "du plus grand nombre d'avis au plus petit",
    "negatifs": "de la plus forte part d'avis négatifs à la plus faible",
    "note_asc": "de la plus mauvaise note à la meilleure",
    "note_desc": "de la meilleure note à la plus mauvaise",
}

#: Tris qui classent sur un TAUX et non sur un compte.
#:
#: La distinction commande le plancher de volume appliqué plus bas, et elle
#: n'est pas cosmétique : un taux se laisse dominer par les petits effectifs,
#: un compte non.
_TRIS_SUR_TAUX = frozenset({"negatifs", "note_asc", "note_desc"})


# ---------------------------------------------------------------------------
# Seuils — EN PYTHON, jamais dans le prompt
# ---------------------------------------------------------------------------

#: Fenêtre retenue quand la question n'en donne aucune.
#:
#: 30 JOURS ET NON « TOUT L'HISTORIQUE ». « Quelle filiale revient le plus ces
#: jours-ci » porte sur le présent ; répondre sur un corpus qui remonte à 2013
#: répondrait à une autre question, avec des chiffres justes — le pire cas,
#: puisque rien dans la réponse ne signalerait le malentendu. La fenêtre
#: retenue est donc systématiquement rappelée dans la réponse.
JOURS_DEFAUT = 30

#: Fenêtre minimale et maximale acceptées.
#:
#: Le plafond n'est pas une contrainte technique — `StatsFilter` accepte tout
#: l'historique — mais une garantie de sens : au-delà d'un an, un « classement
#: par volume » mesure surtout l'ancienneté d'une fiche dans le corpus.
JOURS_MIN, JOURS_MAX = 1, 365

#: Lignes rendues par défaut, et plafond dur.
#:
#: CINQ, PARCE QUE LA RÉPONSE EST LUE SUR UN TÉLÉPHONE. C'est la leçon déjà
#: payée par le briefing de l'Agent 1 : au-delà, le lecteur saute le bloc
#: entier — y compris la première ligne, qui était celle qui comptait.
LIMITE_DEFAUT, LIMITE_MAX = 5, 10


class QuestionRefusee(Exception):
    """La question ne rentre pas dans le contrat.

    N'est PAS une erreur technique : c'est une réponse, et son message est
    montré tel quel à l'utilisateur. D'où le français, et d'où l'obligation de
    dire ce qui manque plutôt que « paramètre invalide » — un robot qui refuse
    sans expliquer est un robot qu'on cesse d'interroger.
    """


# ---------------------------------------------------------------------------
# Résolution des noms contre le catalogue réel
# ---------------------------------------------------------------------------


def normaliser(texte: str) -> str:
    """Forme comparable d'un nom : sans accents, sans casse, sans espaces doubles.

    LES ACCENTS SONT LE CAS RÉEL, pas une précaution théorique : le catalogue
    contient « Égypte », « Sénégal », « Côte d'Ivoire » et « Guinée », et
    personne ne les tape accentués dans Telegram. Sans cette normalisation,
    « quelle filiale d'Orange en Egypte » ne résout rien et le robot répond
    qu'il ne connaît pas un pays qui est en base.

    La ponctuation est CONSERVÉE : le catalogue contient « e& » et « 9mobile ».
    Les nettoyer réduirait « e& » à « e », qui ne désigne plus rien.
    """
    sans_accent = "".join(
        c for c in unicodedata.normalize("NFD", texte or "")
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(sans_accent.casefold().split())


@dataclass(frozen=True)
class Catalogue:
    """Ce que la base connaît réellement : opérateurs, pays, régions.

    Construit depuis `StatsRepository.filter_options()`, c'est-à-dire depuis la
    MÊME source que la barre de filtres du dashboard. Le robot ne peut donc pas
    répondre sur un périmètre que l'interface ne saurait pas afficher.
    """

    #: nom normalisé -> (identifiant technique, libellé d'origine)
    operateurs: dict[str, tuple[int, str]]
    pays: dict[str, tuple[str, str]]      # -> (iso2, libellé)
    regions: dict[str, tuple[str, str]]   # -> (région, libellé) — la clé EST la valeur

    @classmethod
    def depuis(cls, options: dict) -> "Catalogue":
        return cls(
            operateurs={
                normaliser(o["label"]): (int(o["id"]), o["label"])
                for o in options.get("operators", [])
                if o.get("label")
            },
            pays={
                normaliser(p["label"]): (p["iso2"], p["label"])
                for p in options.get("countries", [])
                if p.get("label") and p.get("iso2")
            },
            regions={
                normaliser(r): (r, r) for r in options.get("regions", []) if r
            },
        )

    # ------------------------------------------------------------ Résolution

    @staticmethod
    def _resoudre(
        index: dict[str, tuple[Any, str]], demande: str, genre: str
    ) -> tuple[Any, str]:
        """Un nom -> une entrée du catalogue, ou un refus explicite.

        L'ÉGALITÉ EXACTE PRIME SUR TOUT LE RESTE, et ce n'est pas un raffinement :
        le catalogue réel contient « Airtel » ET « AirtelTigo », « Guinée »,
        « Guinée-Bissau » et « Guinée équatoriale ». Avec une règle par
        inclusion appliquée d'abord, « Airtel » — l'opérateur le plus cité du
        périmètre après MTN et Orange — deviendrait ambigu et serait refusé.

        L'inclusion ne sert donc qu'en second recours, et seulement si elle
        désigne UNE SEULE entrée : « vodacom sa » trouve « Vodacom », tandis que
        « congo » reste refusé parce qu'il désigne réellement deux pays du
        périmètre (Congo-Brazzaville et RD Congo). Refuser en disant lesquels
        vaut mieux que trancher au hasard — le lecteur ne verrait pas l'erreur.
        """
        cle = normaliser(demande)
        if not cle:
            raise QuestionRefusee(f"Quel {genre} ?")

        exact = index.get(cle)
        if exact:
            return exact

        proches = sorted(
            (libelle for nom, (_, libelle) in index.items() if cle in nom or nom in cle)
        )
        if len(proches) == 1:
            return index[normaliser(proches[0])]
        if proches:
            raise QuestionRefusee(
                f"« {demande} » peut désigner plusieurs {genre}s : "
                + ", ".join(proches[:5])
                + ". Précisez lequel."
            )
        raise QuestionRefusee(
            f"Je ne connais pas de {genre} « {demande} » dans le périmètre suivi."
        )

    def operateur(self, nom: str) -> tuple[int, str]:
        return self._resoudre(self.operateurs, nom, "opérateur")

    def pays_(self, nom: str) -> tuple[str, str]:
        return self._resoudre(self.pays, nom, "pays")

    def region(self, nom: str) -> tuple[str, str]:
        return self._resoudre(self.regions, nom, "région")

    # ------------------------------------------------------------ Vocabulaire

    def vocabulaire(self) -> str:
        """Noms connus, tels qu'ils sont écrits en base, pour le prompt.

        LES DONNER AU MODÈLE ÉVITE LE PLUS GROS DE LA CASSE. Sans cette liste,
        il rend l'orthographe de la question (« orange mali », « MTN Nigéria »,
        « ethio telecom ») et la résolution échoue sur des variantes qu'un
        humain aurait acceptées. Avec elle, il rend le libellé exact et la
        résolution est une simple égalité.

        Les filiales n'y figurent PAS, volontairement : une filiale est un
        opérateur ET un pays, deux listes que le modèle a déjà. « Orange Mali »
        se décompose en `operateur=Orange, pays=Mali` sans qu'il faille lui
        apprendre 135 noms de plus — soit un prompt deux fois plus long pour
        une information redondante.
        """
        return (
            "Opérateurs connus : "
            + ", ".join(sorted(libelle for _, libelle in self.operateurs.values()))
            + "\nPays connus : "
            + ", ".join(sorted(libelle for _, libelle in self.pays.values()))
            + "\nRégions connues : "
            + ", ".join(sorted(libelle for _, libelle in self.regions.values()))
        )


# ---------------------------------------------------------------------------
# Demande validée
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Demande:
    """Une question traduite, validée, et prête à être exécutée."""

    intention: str
    niveau: str
    tri: str
    jours: int
    limite: int
    filtre: StatsFilter

    #: Plancher d'avis clients imposé au classement. Voir `_plancher_de_volume`.
    min_avis: int

    #: Périmètre en clair, tel qu'il sera rappelé dans la réponse. Une réponse
    #: sans son périmètre est une réponse qu'on ne peut pas vérifier — c'est la
    #: règle que `StatsFilter.describe()` applique déjà à tous les écrans.
    portee: str

    def as_dict(self) -> dict:
        """Trace journalisable : ce que le robot a compris, en clair.

        C'est ce qui rend une réponse contestable. « Le robot s'est trompé »
        n'est débogable que si l'on peut relire les paramètres qu'il a retenus,
        sans avoir à rejouer l'appel au modèle — qui, lui, ne rendra pas deux
        fois la même chose.
        """
        return {
            "intention": self.intention,
            "niveau": self.niveau,
            "tri": self.tri,
            "jours": self.jours,
            "limite": self.limite,
            "min_avis": self.min_avis,
            "portee": self.portee,
            "filtre": self.filtre.describe(),
        }


def _entier(valeur: Any, defaut: int, mini: int, maxi: int) -> int:
    """Borne un entier venu du modèle, sans jamais lever.

    LE MODÈLE REND CE QU'IL VEUT : `null`, `"30"`, `"trente"`, `9999`, `-1`.
    Lever sur chacun de ces cas transformerait une question compréhensible en
    refus. On préfère borner et le dire dans la réponse : la fenêtre effective
    est de toute façon rappelée au lecteur, qui verra qu'elle n'est pas celle
    qu'il croyait avoir demandée.
    """
    try:
        entier = int(valeur)
    except (TypeError, ValueError):
        return defaut
    return max(mini, min(maxi, entier))


#: Alias public de `_entier`, pour les contrats voisins.
#:
#: Le brief de campagne (`agents/campagne.py`) doit borner de la MÊME façon ce
#: que le modèle rend — silencieusement, sans lever. Une seconde implémentation
#: divergerait : celle qui lèverait sur `"trente"` transformerait une demande
#: compréhensible en refus, et personne ne verrait que les deux contrats ne se
#: comportent plus pareil.
borner_entier = _entier


def _plancher_de_volume(tri: str) -> int:
    """Avis clients exigés pour qu'une ligne entre dans un classement.

    MESURÉ SUR LE CORPUS, LE 12 AOÛT 2026. « Quels sont les 3 pays où les
    clients sont les plus mécontents ? », sur 30 jours et sans plancher, répond
    Madagascar (100 % de négatifs sur 2 avis), Mali, puis Niger (75 % sur 4
    avis) : deux réponses sur trois reposent sur moins de cinq avis. Avec le
    plancher, la même question répond Mali (222 avis), Tunisie (30) et Algérie
    (94) — des pays sur lesquels une décision est possible.

    C'est la même mécanique qui avait déjà fait remonter Vodacom South Africa
    en tête d'un classement de variation avec −75,2 points, sa fenêtre de
    comparaison ne contenant qu'UN avis. Le calcul est exact ; l'affirmation
    qu'il suggère est fausse.

    Le seuil n'est pas réinventé ici : c'est `RELIABILITY_MIN_REVIEWS`, le
    plancher de publiabilité d'un taux que le dashboard applique déjà. Une
    seconde valeur, même bien choisie, finirait par diverger de la première et
    le robot contredirait l'écran.

    SUR UN TRI PAR VOLUME, LE PLANCHER TOMBE À UN SEUL AVIS. Classer par nombre
    d'avis ne peut pas être faussé par un petit effectif — celui-ci est, par
    construction, en bas de liste — et exiger davantage ferait disparaître de la
    réponse des filiales que la question désigne explicitement. Un plancher à
    zéro serait en revanche une réponse bavarde : le classement se remplirait
    d'entités à zéro avis client (seule la presse les mentionne), c'est-à-dire
    de lignes qui répondent « aucun » à une question qui demande « le plus ».
    """
    from reviews.storage.stats_repository import RELIABILITY_MIN_REVIEWS

    return RELIABILITY_MIN_REVIEWS if tri in _TRIS_SUR_TAUX else 1


def valider(brut: dict, catalogue: Catalogue) -> Demande:
    """Traduit la sortie du modèle en demande exécutable, ou refuse.

    Args:
        brut: l'objet JSON rendu par le modèle. Rien n'y est présumé : chaque
            champ est vérifié, borné ou résolu.
        catalogue: ce que la base connaît réellement.

    Raises:
        QuestionRefusee: message en français, montré tel quel à l'utilisateur.
    """
    if not isinstance(brut, dict):
        raise QuestionRefusee("Je n'ai pas compris la question.")

    # --- Intention -----------------------------------------------------------
    #
    # Le champ le plus important, et le seul dont le modèle ne peut pas
    # « approximer » la valeur : une intention inconnue signifie que la question
    # sort du périmètre, pas qu'il faut se rabattre sur la plus proche. C'est la
    # limite annoncée d'avance — « combien Orange va-t-il perdre d'abonnés ? »
    # n'a aucune donnée derrière, et doit recevoir un « je ne sais pas ».
    intention = str(brut.get("intention") or "").strip()
    if intention not in INTENTIONS:
        raise QuestionRefusee(
            brut.get("pourquoi")
            or "Je ne sais répondre qu'aux questions de classement pour l'instant "
            "— par exemple : « quelle filiale d'Orange revient le plus ces jours-ci ? »"
        )

    # --- Niveau et tri : listes blanches, sans repli silencieux ---------------
    #
    # Un repli sur le défaut serait pire qu'un refus : le robot répondrait à une
    # autre question que celle posée, avec l'assurance d'une réponse chiffrée.
    niveau = str(brut.get("niveau") or "subsidiary").strip()
    if niveau not in NIVEAUX or niveau not in LEVELS:
        raise QuestionRefusee(
            f"Je ne sais pas classer par « {niveau} ». "
            "Je sais comparer des filiales, des opérateurs, des pays ou des régions."
        )

    tri = str(brut.get("tri") or "volume").strip()
    if tri not in TRIS:
        raise QuestionRefusee(
            f"Je ne sais pas trier par « {tri} ». Je sais classer par volume "
            "d'avis, par part d'avis négatifs ou par note."
        )

    # --- Périmètre : chaque nom est résolu, jamais recopié --------------------
    operateurs: tuple[int, ...] = ()
    pays: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    morceaux: list[str] = []

    if brut.get("operateur"):
        op_id, op_label = catalogue.operateur(str(brut["operateur"]))
        operateurs = (op_id,)
        morceaux.append(op_label)

    if brut.get("pays"):
        iso2, pays_label = catalogue.pays_(str(brut["pays"]))
        pays = (iso2,)
        morceaux.append(pays_label)

    if brut.get("region"):
        region, region_label = catalogue.region(str(brut["region"]))
        regions = (region,)
        morceaux.append(region_label)

    jours = _entier(brut.get("jours"), JOURS_DEFAUT, JOURS_MIN, JOURS_MAX)
    limite = _entier(brut.get("limite"), LIMITE_DEFAUT, 1, LIMITE_MAX)

    morceaux.append(f"{jours} derniers jours")

    return Demande(
        intention=intention,
        niveau=niveau,
        tri=tri,
        jours=jours,
        limite=limite,
        min_avis=_plancher_de_volume(tri),
        portee=" · ".join(morceaux),
        filtre=StatsFilter(
            days=jours,
            countries=pays,
            regions=regions,
            operators=operateurs,
        ),
    )
