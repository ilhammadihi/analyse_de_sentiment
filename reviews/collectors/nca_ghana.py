"""
Collecteur d'abonnés mobile PAR OPÉRATEUR — NCA Ghana, bulletin statistique trimestriel.

D'OÙ VIENT LA DONNÉE, ET COMMENT ELLE A ÉTÉ TROUVÉE
    Découverte le 24 août 2026 en cherchant une source récente et sourcée
    pour compléter le Kenya/l'Afrique du Sud/l'Égypte, à la demande explicite
    de recherches réelles et tracées plutôt que d'estimations. Le National
    Communications
    Authority publie un « Statistical Bulletin » PDF chaque trimestre, avec
    en Annexe A un vrai tableau texte (`Table 2 : Mobile Voice Subscriptions
    and Market Share per Operator`), CINQ trimestres glissants, PAR
    OPÉRATEUR — même profil de fiabilité qu'ARCEP Bénin, et plus frais
    (T2 2025 au 24 août 2026, contre annuel-2024 pour ARCEP Bénin).

CE QUE LE TABLEAU CONTIENT VRAIMENT — VÉRIFIÉ, PAS SUPPOSÉ
    `pdfplumber.extract_tables()` rend ce tableau précis avec le nom de
    l'opérateur SEULEMENT sur sa ligne « Subscriptions », et `None` sur la
    ligne « Market Share (%) » qui suit — la fusion verticale de la cellule
    « MTN » sur ses deux lignes est éclatée en deux lignes de table, la
    seconde héritant un blanc. Le parseur suit donc un OPÉRATEUR COURANT :
    une ligne qui porte un nom d'opérateur en colonne 0 fixe l'opérateur
    courant ; les valeurs ne sont retenues QUE sur une ligne « Subscriptions »
    (colonne 1), jamais sur « Market Share (%) », qui est un pourcentage et
    non un décompte.

    Le tableau est repéré par son EN-TÊTE (« Mobile Network Operator » en
    première colonne), jamais par un numéro de page : l'Annexe A rassemble
    trente tableaux numérotés, et leur position glisse d'une édition à
    l'autre selon ce qui a été ajouté ailleurs dans le document.

CE QUE « AT » ET « TELECEL » DÉSIGNENT
    « AT » est le nom commercial d'AirtelTigo depuis la fusion Airtel/Tigo au
    Ghana — `dim_operator.code = 'airteltigo'`, déjà rattaché à cette
    filiale (migration 003).

    « Telecel » DÉSIGNE L'ANCIEN VODAFONE GHANA. Le Telecel Group a acquis
    70 % de Vodafone Ghana (Ghana Telecommunications Company Limited) en
    février 2023 et achevé le rebranding en mars 2024 — même entité, mêmes
    numéros (020/050), même licence, confirmé par recoupement presse le 24
    août 2026 (Telecel Group, ConnectingAfrica). `dim_subsidiary` ne connaît
    encore que « Vodafone Ghana » (code opérateur `vodafone`, pays `GH`) :
    faute d'avoir vérifié s'il fallait migrer cette filiale vers l'entité
    globale `telecel` (déjà utilisée pour Telecel Centrafrique/Zimbabwe) ou
    seulement ajouter un alias, ce collecteur rattache PROVISOIREMENT
    « Telecel » à l'opérateur `vodafone` — la seule clé qui résout
    aujourd'hui. À corriger explicitement, pas en silence, le jour où la
    question aura été tranchée.

L'URL DU PDF N'EST PAS STABLE D'UNE ÉDITION À L'AUTRE
    Comme ARCEP Bénin : chaque bulletin est hébergé sous un chemin daté
    (`/wp-content/uploads/2025/12/Q2-2025-Statistical-Bulletin.pdf`, publié
    ~6 mois après la fin du trimestre couvert). CE LIEN DEVRA ÊTRE
    RECONTRÔLÉ À CHAQUE NOUVELLE ÉDITION.

CE QUE CE MODULE N'EST PAS
    Comme les trois autres collecteurs régulateur, il ne connaît pas le
    modèle dimensionnel : il rend un nom d'opérateur PUBLIÉ PAR LA SOURCE,
    charge à `OperatorMarketRepository` de le résoudre.
"""

import logging
import re
from io import BytesIO
from typing import Optional

import pdfplumber
import requests

logger = logging.getLogger(__name__)

#: Vérifié le 24 août 2026 — À RECONTRÔLER À CHAQUE NOUVELLE ÉDITION (voir la
#: documentation du module).
_URL_PDF = "https://nca.org.gh/wp-content/uploads/2025/12/Q2-2025-Statistical-Bulletin.pdf"

#: Nom publié par le NCA -> code `dim_operator`.
OPERATEURS: dict[str, str] = {
    "MTN": "mtn",
    "Telecel": "vodafone",  # voir « CE QUE "TELECEL" DÉSIGNE » ci-dessus
    "AT": "airteltigo",
}

#: Repère l'en-tête du tableau recherché, insensible à une éventuelle
#: deuxième colonne vide (« Mobile Network Operator », None, « Q2 2024 »…).
_ENTETE_ATTENDUE = "Mobile Network Operator"

#: Trimestre -> (mois, dernier jour du mois) — voir `_periode`.
_FIN_TRIMESTRE = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}

METRIC = "abonnes_voix_mobile"
FREQUENCY = "quarterly"
SOURCE = "nca_ghana"
ISO2 = "GH"

TIMEOUT = 30


class NcaGhanaCollector:
    """Interroge le bulletin statistique trimestriel PDF du NCA ghanéen."""

    def __init__(self, timeout: int = TIMEOUT):
        self.timeout = timeout
        self.logger = logger

    def collect(self) -> tuple[list[dict], list[str]]:
        """Récupère les abonnements voix mobile trimestriels des 3 opérateurs ghanéens.

        Returns:
            (lignes prêtes pour `OperatorMarketRepository.upsert`, erreurs).
        """
        try:
            pdf = self._fetch()
        except Exception as e:  # noqa: BLE001
            return [], [f"NCA Ghana : {e}"]

        table = self._trouver_table(pdf)
        if table is None:
            return [], [
                f"NCA Ghana : tableau « {_ENTETE_ATTENDUE} » introuvable — le "
                "PDF a peut-être changé de forme ou l'URL de l'édition a expiré"
            ]

        lignes = self._normaliser(table)
        self.logger.info(
            "NCA Ghana : %d mesure(s) sur %d opérateur(s), 0 erreur(s)",
            len(lignes), len(OPERATEURS),
        )
        return lignes, []

    # ------------------------------------------------------------- Interne

    def _fetch(self):
        reponse = requests.get(
            _URL_PDF,
            headers={"User-Agent": "Mozilla/5.0 (compatible; dashboard-veille/1.0)"},
            timeout=self.timeout,
        )
        reponse.raise_for_status()
        return pdfplumber.open(BytesIO(reponse.content))

    @staticmethod
    def _trouver_table(pdf) -> Optional[list[list]]:
        for page in pdf.pages:
            for table in page.extract_tables():
                if table and table[0] and table[0][0] == _ENTETE_ATTENDUE:
                    return table
        return None

    @classmethod
    def _normaliser(cls, table: list[list]) -> list[dict]:
        entete = table[0]
        # Colonnes de trimestres : tout ce qui suit les deux premières
        # colonnes (nom d'opérateur, libellé de ligne) — filtré des blancs
        # qu'une éventuelle colonne fusionnée insère.
        trimestres = [c for c in entete[2:] if c]

        lignes: list[dict] = []
        operateur_courant: Optional[str] = None
        for row in table[1:]:
            nom_brut, libelle = row[0], row[1]
            if nom_brut:
                operateur_courant = nom_brut.strip()
            code_operateur = OPERATEURS.get(operateur_courant or "")
            if code_operateur is None or libelle != "Subscriptions":
                continue  # « Market Share (%) », ou opérateur hors périmètre

            valeurs = [v for v in row[2:] if v]
            # ALIGNEMENT DEPUIS LA DROITE, comme ARCEP Bénin (voir sa
            # documentation) : une cellule vide en tête de série (opérateur
            # sans donnée sur les trimestres les plus anciens de la fenêtre
            # glissante) décale sinon silencieusement chaque valeur restante
            # sur le mauvais trimestre.
            trimestres_alignes = trimestres[-len(valeurs):]
            for trimestre, brute in zip(trimestres_alignes, valeurs):
                periode = cls._periode(trimestre)
                valeur = cls._nombre(brute)
                if periode is None or valeur is None:
                    continue
                lignes.append({
                    "operator_code": code_operateur,
                    "iso2": ISO2,
                    "metric": METRIC,
                    "period": periode,
                    "frequency": FREQUENCY,
                    "value": valeur,
                    "source": SOURCE,
                    "source_url": _URL_PDF,
                })
        return lignes

    @staticmethod
    def _periode(brut: str) -> Optional[str]:
        """« Q2 2024 » -> « 2024-06-30 » (DERNIER jour du trimestre — les
        abonnements sont comptés à la clôture du trimestre, pas à son
        ouverture ; voir la même correction sur ANRT Maroc)."""
        m = re.match(r"Q([1-4])\s+(\d{4})", brut.strip())
        if not m:
            return None
        trimestre, annee = int(m.group(1)), m.group(2)
        mois, dernier_jour = _FIN_TRIMESTRE[trimestre]
        return f"{annee}-{mois:02d}-{dernier_jour:02d}"

    @staticmethod
    def _nombre(brut: str) -> Optional[float]:
        """« 30,264,594 » (virgules milliers, convention anglophone) -> 30264594.0."""
        nettoye = brut.replace(",", "")
        try:
            return float(nettoye)
        except (TypeError, ValueError):
            return None
