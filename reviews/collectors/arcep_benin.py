"""
Collecteur d'abonnés mobile PAR OPÉRATEUR — ARCEP Bénin, tableau de bord annuel.

D'OÙ VIENT LA DONNÉE, ET POURQUOI ANNUEL SEULEMENT
    L'ARCEP Bénin publie un « Observatoire de la téléphonie mobile — Tableau
    de bord annuel » en PDF, avec quatre ans d'historique et un vrai tableau
    texte (pas un graphique, contrairement au marché en volume de la CA
    Kenya — vérifié le 23 août 2026 en cherchant sur le site, aucune page de
    parc trimestriel n'a été trouvée).
    C'est la SEULE cadence disponible pour ce régulateur à ce jour : le site
    publie par ailleurs des indicateurs hebdomadaires de qualité de service
    (taux de succès, débit) sur `arcep.bj/performances-mtn-et-moov/`, mais
    RIEN d'équivalent en fréquence infra-annuelle pour les abonnés — vérifié
    en cherchant sur le site le 23 août 2026, aucune page de parc trimestriel
    n'a été trouvée. Un régulateur mensuel/trimestriel (NCC Nigeria, ANRT
    Maroc) reste préférable ; celui-ci l'est déjà sur la Banque Mondiale/UIT,
    qui ne descend jamais sous le pays.

CE QUE LE FICHIER CONTIENT VRAIMENT — VÉRIFIÉ LE 23 AOÛT 2026
    Page 3 du PDF (`Tableau 1 : Abonnements mobiles actifs`), colonnes
    DESIGNATIONS / 2020 / 2021 / 2022 / 2023 / 2024, trois lignes opérateur
    (SPACETEL BENIN, MOOV AFRICA BENIN, CELTIIS) plus un total.

    PIÈGE MESURÉ, À NE PAS RETESTER : `pdfplumber.extract_tables()` rend les
    cellules fusionnées comme des colonnes `None`/`''` supplémentaires — la
    ligne d'en-tête brute compte ONZE cellules pour CINQ années. Il faut
    filtrer les blancs avant d'aligner quoi que ce soit.

    SECOND PIÈGE, PLUS SUBTIL : CELTIIS n'a lancé qu'en 2022, donc sa ligne ne
    rend QUE TROIS valeurs après filtrage — mais ALIGNÉES SUR LES TROIS
    DERNIÈRES ANNÉES (2022, 2023, 2024), pas les trois premières. Zipper
    naïvement `zip(annees, valeurs)` aurait attribué son parc 2022 à l'année
    2020. On aligne donc depuis LA DROITE : `annees[-len(valeurs):]`.

CE QUE « SPACETEL BENIN » DÉSIGNE
    C'est la raison sociale sous laquelle MTN opère au Bénin — confirmé le 23
    août 2026 par recoupement presse (« Spacetel Bénin Sa (MTN) », La
    Nouvelle Tribune, sanctions ARCEP 2017), pas deviné. `dim_subsidiary` ne
    connaît que « MTN Bénin » (code opérateur `mtn`, pays `BJ`).

L'URL DU PDF N'EST PAS STABLE D'UNE ÉDITION À L'AUTRE
    Contrairement à la page NCC (même URL, contenu qui glisse) ou au jeu de
    données ANRT (même ressource, republiée), l'ARCEP Bénin héberge chaque
    tableau de bord annuel sous un chemin daté
    (`/wp-content/uploads/2025/04/...`). CE LIEN DEVRA ÊTRE RECONTRÔLÉ À LA
    PROCHAINE ÉDITION — c'est le premier endroit à vérifier si la collecte
    échoue sans erreur réseau.

CE QUE CE MODULE N'EST PAS
    Comme les deux autres collecteurs régulateur, il ne connaît pas le modèle
    dimensionnel : il rend un nom d'opérateur PUBLIÉ PAR LA SOURCE, charge à
    `OperatorMarketRepository` de le résoudre.
"""

import logging
import re
from io import BytesIO
from typing import Optional

import pdfplumber
import requests

logger = logging.getLogger(__name__)

#: Vérifié le 23 août 2026 — À RECONTRÔLER À CHAQUE NOUVELLE ÉDITION (voir la
#: documentation du module).
_URL_PDF = (
    "https://arcep.bj/wp-content/uploads/2025/04/"
    "TB-Annuel_T%C3%A9l%C3%A9phonie-Mobile.pdf"
)

#: Nom publié par l'ARCEP -> code `dim_operator`. Fermée, comme dans les
#: autres collecteurs régulateur : un nom absent d'ici est ignoré.
OPERATEURS: dict[str, str] = {
    "SPACETEL BENIN": "mtn",
    "MOOV AFRICA BENIN": "moov_africa",
    "CELTIIS": "celtiis",
}

#: Titre exact de la table recherchée, tel qu'imprimé dans le PDF.
_TITRE_TABLE = "Tableau 1"

METRIC = "abonnes_mobiles_actifs"
FREQUENCY = "annual"
SOURCE = "arcep_benin"
ISO2 = "BJ"

TIMEOUT = 30


class ArcepBeninCollector:
    """Interroge le tableau de bord annuel PDF de l'ARCEP Bénin."""

    def __init__(self, timeout: int = TIMEOUT):
        self.timeout = timeout
        self.logger = logger

    def collect(self) -> tuple[list[dict], list[str]]:
        """Récupère le parc mobile annuel des 3 opérateurs béninois.

        Returns:
            (lignes prêtes pour `OperatorMarketRepository.upsert`, erreurs).
        """
        try:
            pdf = self._fetch()
        except Exception as e:  # noqa: BLE001
            return [], [f"ARCEP Bénin : {e}"]

        table = self._trouver_table(pdf)
        if table is None:
            return [], [
                f"ARCEP Bénin : « {_TITRE_TABLE} » introuvable — le PDF a "
                "peut-être changé de forme ou l'URL de l'édition a expiré"
            ]

        lignes = self._normaliser(table)
        self.logger.info(
            "ARCEP Bénin : %d mesure(s) sur %d opérateur(s), 0 erreur(s)",
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
        """Cherche la page qui titre `_TITRE_TABLE` et en rend la première
        table détectée par `pdfplumber`."""
        for page in pdf.pages:
            texte = page.extract_text() or ""
            if _TITRE_TABLE in texte:
                tables = page.extract_tables()
                if tables:
                    return tables[0]
        return None

    @classmethod
    def _normaliser(cls, table: list[list]) -> list[dict]:
        entete = cls._filtrer_blancs(table[0])
        if not entete or entete[0].strip().upper() != "DESIGNATIONS":
            return []
        annees = entete[1:]

        lignes: list[dict] = []
        for row in table[1:]:
            cellules = cls._filtrer_blancs(row)
            if not cellules:
                continue
            nom, valeurs = cellules[0], cellules[1:]
            code_operateur = OPERATEURS.get(nom.strip().upper())
            if code_operateur is None or not valeurs:
                continue  # ligne "Total…" / continuation d'étiquette, ignorée

            # ALIGNEMENT DEPUIS LA DROITE : un opérateur entré tardivement
            # (CELTIIS) rend moins de valeurs que d'années — voir la
            # documentation du module.
            annees_alignees = annees[-len(valeurs):]
            for annee, brute in zip(annees_alignees, valeurs):
                valeur = cls._nombre(brute)
                if valeur is None or not re.match(r"^\d{4}$", annee.strip()):
                    continue
                lignes.append({
                    "operator_code": code_operateur,
                    "iso2": ISO2,
                    "metric": METRIC,
                    # DERNIER jour de l'année, pas le premier : « Abonnements
                    # mobiles ACTIFS » d'une année désigne le parc à fin
                    # d'année. Stocker le 1er janvier ferait paraître la
                    # donnée près d'un an plus ancienne qu'elle ne l'est.
                    "period": f"{annee.strip()}-12-31",
                    "frequency": FREQUENCY,
                    "value": valeur,
                    "source": SOURCE,
                    "source_url": _URL_PDF,
                })
        return lignes

    @staticmethod
    def _filtrer_blancs(row: list) -> list[str]:
        """Retire les colonnes `None`/`''` qu'`extract_tables()` insère pour
        chaque cellule fusionnée — voir la documentation du module."""
        return [str(v).strip() for v in row if v not in (None, "")]

    @staticmethod
    def _nombre(brut: str) -> Optional[float]:
        """« 8 860 782 » (espaces normales ou insécables) -> 8860782.0."""
        nettoye = re.sub(r"[\s  ]", "", brut)
        try:
            return float(nettoye)
        except (TypeError, ValueError):
            return None
