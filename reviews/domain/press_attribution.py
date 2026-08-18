"""
À qui appartient un article de presse ?

POURQUOI CETTE RÈGLE VIT DANS LE DOMAINE
    Trois appelants en ont besoin : le collecteur `press_feed`, le collecteur
    `rss_feed`, et la ré-attribution du corpus déjà collecté. Écrite trois fois,
    elle divergerait — et une divergence ici produit des articles rangés sous
    des opérateurs différents selon le chemin qui les a fait entrer.

LES QUATRE ÉTATS, ET CE QU'ILS VALENT

    CONFIRMED     l'article nomme la filiale à laquelle il est déjà rattaché.
    REATTRIBUTED  il en nomme une autre : c'est celle-là qui compte.
    GROUP         il nomme un opérateur mais AUCUN de ses pays. « Orange Money
                  réduit ses frais de retrait de 1 % » concerne toutes les
                  filiales Orange et aucune en particulier. Information réelle,
                  qu'aucune filiale ne peut s'attribuer sans mentir.
    NOISE         il ne nomme personne du périmètre. « Les oranges, un luxe
                  pour les Marocains. »

    Distinguer GROUP de NOISE n'est pas un raffinement : les confondre donnait
    39 % de déchet apparent là où 15 % du corpus est une actualité de groupe
    parfaitement exploitable.

LA RÈGLE DU MARQUEUR DE PAYS
    Pour un opérateur mono-pays, son nom suffit. Pour un opérateur multi-pays,
    le nom NE SUFFIT PAS : sans marqueur de pays, un article sur MTN Ghana
    serait imputé aux dix-sept filiales MTN du périmètre. C'est la même règle
    que celle éprouvée dans `press_feed`, dont le commentaire cite le cas réel
    qui l'a imposée.
"""

import re
import unicodedata
from typing import Iterable, Optional

from reviews.domain.press_relevance import est_pertinent

#: Version de la règle. Écrite sur chaque ligne jugée, pour pouvoir rejouer un
#: tri après correction sans confondre deux verdicts incomparables.
#:
#: v2 : contrôle de vocabulaire ajouté sur les actualités de groupe.
#: v3 : ce contrôle passe EN PREMIER et vaut pour tous les articles — en v2 il
#: était contourné par « <média nommé Orange> + <pays d'Orange> », qui produisait
#: une filiale « confirmée » à partir d'un article d'archéologie.
ATTRIBUTION_VERSION = 3

CONFIRMED = "confirmed"
REATTRIBUTED = "reattributed"
GROUP = "group"
NOISE = "noise"

#: Variantes d'apostrophe et de tiret rencontrées dans la presse en ligne.
#:
#: MESURÉ, PAS SUPPOSÉ. Sur 363 articles mentionnant la Côte d'Ivoire, le corpus
#: écrit « Côte d’Ivoire » (apostrophe typographique) 502 fois contre 188 pour
#: l'apostrophe droite. Les marqueurs de configuration emploient tous
#: l'apostrophe droite : sans unification, le marqueur ne s'appariait que sur
#: 90 articles sur 363, et les 273 autres devenaient orphelins — pour un
#: caractère.
_SEPARATEURS = {
    "’": "'", "‘": "'", "ʼ": "'", "`": "'", "´": "'",
    "‐": " ", "‑": " ", "‒": " ", "–": " ", "—": " ", "-": " ",
}
_TRAD = str.maketrans(_SEPARATEURS)


def normalize(text: str) -> str:
    """Minuscules, sans accents, séparateurs unifiés.

    Appliquée AU MARQUEUR ET AU TEXTE, ce qui rend l'unification sûre :
    remplacer les tirets par des espaces ne peut pas casser « Guinée-Bissau »,
    puisque le marqueur subit la même transformation avant compilation.
    """
    decompose = unicodedata.normalize("NFKD", text or "")
    sans_accents = "".join(c for c in decompose if not unicodedata.combining(c))
    return " ".join(sans_accents.translate(_TRAD).lower().split())


def compile_matchers(matchers: Iterable[dict]) -> dict[str, dict]:
    """Pré-compile les expressions, groupées par opérateur.

    `\\b` de part et d'autre : sans lui, « Orange » se déclenche sur « oranges »
    — le cas n'est pas théorique, « Les oranges, un luxe pour les Marocains »
    était rattaché à Orange Maroc.
    """
    groupes: dict[str, dict] = {}
    for m in matchers:
        groupe = groupes.setdefault(
            m["operator"],
            {
                "operator": re.compile(r"\b" + re.escape(normalize(m["operator"])) + r"\b"),
                "filiales": [],
            },
        )
        groupe["filiales"].append(
            {
                "name": m["name"],
                "iso2": m["iso2"],
                "countries": [
                    re.compile(r"\b" + re.escape(normalize(c)) + r"\b")
                    for c in m["country_markers"]
                ],
            }
        )
    return groupes


def operators_named(groupes: dict[str, dict], haystack: str) -> list[str]:
    """Opérateurs nommés, sans exiger de marqueur de pays."""
    return [nom for nom, g in groupes.items() if g["operator"].search(haystack)]


def subsidiaries_named(
    groupes: dict[str, dict],
    haystack: str,
    feed_iso2: Optional[str] = None,
) -> list[str]:
    """Filiales réellement nommées par l'article.

    Le texte prime toujours sur le pays d'édition du flux : un titre
    sud-africain qui parle de MTN Nigeria concerne MTN Nigeria. Le pays
    d'édition ne sert de repli que si l'article ne nomme aucun pays de
    l'opérateur — et vaut `None` pour Google News, qui n'a pas de flux
    d'origine identifiable.
    """
    trouvees: list[str] = []
    for groupe in groupes.values():
        if not groupe["operator"].search(haystack):
            continue
        filiales = groupe["filiales"]
        mono = [f["name"] for f in filiales if not f["countries"]]
        if mono:
            trouvees.extend(mono)
            continue
        cites = [
            f["name"] for f in filiales
            if any(rx.search(haystack) for rx in f["countries"])
        ]
        if cites:
            trouvees.extend(cites)
        elif feed_iso2:
            trouvees.extend(f["name"] for f in filiales if f["iso2"] == feed_iso2)
    return trouvees


def classify(
    groupes: dict[str, dict],
    title: str,
    text: str,
    current_subsidiary: Optional[str] = None,
    feed_iso2: Optional[str] = None,
) -> tuple[str, list[str], list[str]]:
    """Rend (état, filiales nommées, opérateurs nommés)."""
    # LE VOCABULAIRE PASSE EN PREMIER, ET C'EST UNE CORRECTION.
    #
    # Il n'a d'abord été appliqué qu'aux articles sans filiale identifiée. C'est
    # insuffisant : le nom d'un opérateur apparaît souvent dans le nom du MÉDIA,
    # et si l'article mentionne par ailleurs un pays où cet opérateur est
    # présent, la reconnaissance conclut à une filiale — avec assurance, et à
    # tort.
    #
    #   « Le sarcophage de Toutânkhamon en Égypte — Orange Actualités »
    #     → « Orange » (le média) + « Égypte » = Orange Égypte, confirmé
    #   « Attaque au Niger : Macron dénonce un attentat — Orange Actualités »
    #     → « Orange » (le média) + « Niger » = Orange Niger, confirmé
    #
    # Ces deux articles passaient donc AVANT le contrôle, sans jamais y être
    # soumis. Un article de football, de politique ou d'archéologie n'emploie
    # jamais le vocabulaire du secteur ; un article télécom l'emploie forcément.
    # C'est le seul discriminant qui tienne, et il vaut pour tout le monde.
    if not est_pertinent(title, text):
        return NOISE, [], []

    haystack = normalize(f"{title or ''} {text or ''}")
    filiales = subsidiaries_named(groupes, haystack, feed_iso2)
    if filiales:
        etat = CONFIRMED if current_subsidiary in filiales else REATTRIBUTED
        return etat, filiales, operators_named(groupes, haystack)
    operateurs = operators_named(groupes, haystack)
    if operateurs:
        return GROUP, [], operateurs
    return NOISE, [], []
