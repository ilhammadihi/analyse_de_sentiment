"""
Collecteur d'abonnés GSM PAR OPÉRATEUR — Nigerian Communications Commission.

POURQUOI CE COLLECTEUR EXISTE, ALORS QUE `market_data.py` EN COLLECTE DÉJÀ
    `market_data.py` (Banque Mondiale / UIT) est PAR PAYS et ANNUEL — voir sa
    documentation. L'encadrant a demandé une vue par opérateur avec des
    données récentes ; aucune des deux contraintes n'est soluble depuis cette
    source, ni depuis GSMA Intelligence (vérifié le 23 août 2026 : plateforme
    de données entièrement sous abonnement, `data.gsmaintelligence.com`
    redirige en 302 vers une page de connexion, aucune donnée n'est servie
    sans compte). Seuls les RÉGULATEURS nationaux descendent au niveau
    opérateur avec une cadence infra-annuelle.

    Le NCC nigérian a été retenu comme PREMIER régulateur intégré parce que
    c'est le seul vérifié à date : page publique, sans authentification, sans
    blocage, avec un historique mensuel glissant sur 12 mois et une mise à
    jour effectivement récente (mai 2026 constaté le 23 août 2026, contre 2024
    pour la source pays). D'autres régulateurs identifiés (ANRT Maroc, CA
    Kenya, ARTP Sénégal, ARCEP Bénin) restent à intégrer — chacun avec son
    propre format et sa propre cadence, voir la note sur `frequency` dans
    `025_operator_market_indicators.sql`.

D'OÙ VIENT LA DONNÉE, ET POURQUOI CE N'EST PAS UN TABLEAU HTML À PARSER
    La page ne rend pas un <table> exploitable : les chiffres alimentent des
    graphiques et vivent dans un bloc JSON inline (Drupal), sous une clé de
    widget fixe. Vérifié le 23 août 2026 en récupérant la page brute :
    `"SUBSCRIBER BY OPERATOR (GSM)":{"title":"GSM",...,"categories":[...12
    mois...],"series":[{"category":"SUBSGSM","name":"Airtel","data":[...]},
    ...]}`. C'est CE bloc qu'on extrait, pas un autre : la page en contient
    une quinzaine (INTGSM, PORTINC, MKTGEN…) dont les valeurs ne sont pas les
    mêmes grandeurs — `INTGSM` par exemple rend pour Airtel 55,9 M quand
    `SUBSGSM` en rend 65,4 M pour le même mois. SEUL `SUBSGSM` a été confronté
    aux chiffres publiés par la presse nigériane (MTN 96 977 835 en mai 2026)
    et confirmé exact.

CE QUE « T2 » DÉSIGNE, ET POURQUOI CE N'EST PAS UNE ERREUR DE RATTACHEMENT
    Le NCC nomme le quatrième opérateur GSM « T2 » sur cette page (héritage de
    sa licence). Son volume d'abonnés (3,5 M en mai 2026, le plus petit des
    quatre) et sa trajectoire correspondent à 9mobile, seul quatrième
    opérateur GSM nigérian couvert par `dim_operator` (code `nine_mobile`).
    Confirmé par recoupement presse le 23 août 2026, PAS deviné.

CE QUE CE MODULE N'EST PAS
    Comme `market_data.py`, ce n'est pas un `BaseCollector` : il ne produit
    aucun `Review`. Il ne connaît pas non plus le modèle dimensionnel — il
    rend un opérateur par SON NOM PUBLIÉ PAR LA SOURCE (`Airtel`, `T2`…),
    charge à `OperatorMarketRepository` de le résoudre en `subsidiary_id`.
"""

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_URL = "https://ncc.gov.ng/market-data-reports/industry-statistics"

#: Clé exacte du widget dans le JSON inline de la page. VÉRIFIÉE le 23 août
#: 2026 ; si le NCC refond la page, cette clé est le premier endroit à
#: recontrôler (voir `python -m reviews.cli ncc-nigeria` pour un essai à vide).
_CLE_WIDGET = '"SUBSCRIBER BY OPERATOR (GSM)"'

#: Nom publié par la source -> code `dim_operator`. Volontairement fermée :
#: VITEL et Visafone apparaissent dans la série mais n'ont plus de données
#: (opérateurs éteints, cellules vides) et TOTAL n'est pas un opérateur.
#: Un nom absent d'ici est ignoré plutôt que de faire échouer la collecte.
OPERATEURS: dict[str, str] = {
    "Airtel": "airtel",
    "Globacom": "glo",
    "MTN": "mtn",
    "T2": "nine_mobile",
}

_MOIS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

#: Un seul indicateur pour commencer — voir `market_data.py` sur le choix
#: délibéré de rester court. `SUBSGSM` est le seul confronté à une source
#: indépendante (la presse) ; les autres blocs du JSON (marché, portabilité,
#: internet fixe) attendent une vérification avant d'être ajoutés.
METRIC = "abonnes_gsm"
FREQUENCY = "monthly"
SOURCE = "ncc_nigeria"

TIMEOUT = 20


class NccNigeriaCollector:
    """Interroge la page « Industry Statistics » du NCC nigérian."""

    def __init__(self, timeout: int = TIMEOUT):
        self.timeout = timeout
        self.logger = logger

    def collect(self) -> tuple[list[dict], list[str]]:
        """Récupère les abonnés GSM mensuels des 4 opérateurs nigérians.

        Returns:
            (lignes prêtes pour `OperatorMarketRepository.upsert`, erreurs).
            Une ligne : operator_code, iso2, metric, period (str AAAA-MM-01),
            frequency, value, source, source_url.
        """
        try:
            html = self._fetch()
        except Exception as e:  # noqa: BLE001
            return [], [f"NCC Nigeria : {e}"]

        bloc = self._extraire_bloc(html)
        if bloc is None:
            return [], [
                f"NCC Nigeria : clé {_CLE_WIDGET} introuvable — la page a "
                "peut-être changé de structure"
            ]

        mois = self._extraire_mois(bloc)
        if not mois:
            return [], ["NCC Nigeria : aucune période lisible dans le bloc"]

        lignes: list[dict] = []
        erreurs: list[str] = []
        for nom_source, code_operateur in OPERATEURS.items():
            valeurs = self._extraire_serie(bloc, nom_source)
            if valeurs is None:
                erreurs.append(f"NCC Nigeria : opérateur {nom_source} absent de la série")
                continue
            if len(valeurs) != len(mois):
                erreurs.append(
                    f"NCC Nigeria : {nom_source} a {len(valeurs)} valeur(s) pour "
                    f"{len(mois)} mois — série ignorée"
                )
                continue
            for periode, brute in zip(mois, valeurs):
                valeur = self._nombre(brute)
                if valeur is None:
                    continue  # cellule vide ("") : mois sans donnée publiée
                lignes.append({
                    "operator_code": code_operateur,
                    "iso2": "NG",
                    "metric": METRIC,
                    "period": periode,
                    "frequency": FREQUENCY,
                    "value": valeur,
                    "source": SOURCE,
                    "source_url": _URL,
                })

        self.logger.info(
            "NCC Nigeria : %d mesure(s) sur %d opérateur(s), %d erreur(s)",
            len(lignes), len(OPERATEURS), len(erreurs),
        )
        return lignes, erreurs

    # ------------------------------------------------------------- Interne

    def _fetch(self) -> str:
        reponse = requests.get(
            _URL,
            headers={"User-Agent": "Mozilla/5.0 (compatible; dashboard-veille/1.0)"},
            timeout=self.timeout,
        )
        reponse.raise_for_status()
        return reponse.text

    @staticmethod
    def _extraire_bloc(html: str) -> Optional[str]:
        """Isole le JSON du widget « SUBSCRIBER BY OPERATOR (GSM) ».

        PAR COMPTAGE D'ACCOLADES, PAS PAR REGEX SUR TOUT LE DOCUMENT : la page
        contient une quinzaine de blocs structurellement identiques
        (INTGSM, PORTINC, MKTGEN…) ; une regex non-gourmande sur tout le
        document risquerait de s'arrêter sur la fermeture d'un AUTRE bloc si
        deux widgets se suivent sans texte entre eux. Compter les accolades
        depuis l'ouverture de CE bloc garantit qu'on récupère exactement son
        contenu, ni plus ni moins.
        """
        debut = html.find(_CLE_WIDGET)
        if debut == -1:
            return None
        ouverture = html.find("{", debut + len(_CLE_WIDGET))
        if ouverture == -1:
            return None

        profondeur = 0
        for i in range(ouverture, len(html)):
            if html[i] == "{":
                profondeur += 1
            elif html[i] == "}":
                profondeur -= 1
                if profondeur == 0:
                    return html[ouverture:i + 1]
        return None

    @staticmethod
    def _extraire_mois(bloc: str) -> list[str]:
        """Convertit `["May'26","Apr'26",...]` en `["2026-05-01","2026-04-01",...]`.

        Les abréviations sont en anglais, la source étant nigériane. Un
        libellé qu'on ne sait pas convertir est écarté plutôt que de fausser
        l'alignement avec les séries de valeurs.
        """
        m = re.search(r'"categories":\[([^\]]*)\]', bloc)
        if not m:
            return []
        libelles = re.findall(r'"([^"]*)"', m.group(1))
        periodes = []
        for lib in libelles:
            lisible = lib.replace("\\u0027", "'")
            match = re.match(r"([A-Za-z]{3})'(\d{2})", lisible)
            if not match:
                continue
            abrev, aa = match.groups()
            mois_num = _MOIS.get(abrev[:3].title())
            if mois_num is None:
                continue
            periodes.append(f"20{aa}-{mois_num:02d}-01")
        return periodes

    @staticmethod
    def _extraire_serie(bloc: str, nom_operateur: str) -> Optional[list[str]]:
        """Rend la série brute d'un opérateur, ex. `["65450286", "", "63629101"]`.

        `data` est une liste plate de nombres et de chaînes vides — jamais de
        virgule dans une valeur — donc un simple split suffit, pas besoin de
        reparser un JSON complet.
        """
        m = re.search(
            r'"category":"SUBSGSM","name":"' + re.escape(nom_operateur) + r'","data":\[([^\]]*)\]',
            bloc,
        )
        if not m:
            return None
        if not m.group(1).strip():
            return []
        return [v.strip().strip('"') for v in m.group(1).split(",")]

    @staticmethod
    def _nombre(brute: str) -> Optional[float]:
        if brute in ("", None):
            return None
        try:
            return float(brute)
        except (TypeError, ValueError):
            return None
