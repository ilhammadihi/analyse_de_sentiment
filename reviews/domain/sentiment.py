"""
Analyse de sentiment basée sur un lexique (français + anglais).
Approche légère type VADER : lexique pondéré, négation, intensificateurs,
ponctuation. Aucune dépendance externe, aucun modèle à télécharger.
"""

import json
import re
import unicodedata
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from reviews.domain.models import SentimentEnum

# ---------------------------------------------------------------------------
# Nettoyage / préparation du texte
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}")  # "supeeeer" -> "supeer"

# Le bloc arabe (U+0600–U+06FF) fait partie des jetons reconnus.
#
# Sans lui, un avis égyptien, marocain, algérien, tunisien ou soudanais ne
# produisait AUCUN jeton, donc aucun sentiment : cinq pays du périmètre
# ressortaient systématiquement « neutre ». C'était l'une des causes mesurées
# du taux de neutres à 51 %.
_TOKEN_RE = re.compile(r"[a-zà-öø-ÿ']+|[؀-ۿ]+|[!?]+|[,.;:]", re.IGNORECASE)
_BOUNDARY_TOKENS = {",", ".", ";", ":"}


def clean_text(text: str) -> str:
    """Nettoie un texte brut avant analyse (ne modifie pas Review.text)."""
    if not text:
        return ""
    t = _URL_RE.sub(" ", text)
    t = _HTML_TAG_RE.sub(" ", t)
    t = _REPEATED_CHAR_RE.sub(r"\1\1", t)
    t = " ".join(t.split())
    return t.strip()


def _strip_accents(word: str) -> str:
    normalized = unicodedata.normalize("NFKD", word)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def _tokenize(text: str) -> list[str]:
    return [tok.lower() for tok in _TOKEN_RE.findall(text)]


# ---------------------------------------------------------------------------
# Lexique de sentiment (français + anglais, orienté avis clients télécom)
# ---------------------------------------------------------------------------

POSITIVE_WORDS: dict[str, float] = {
    # Général FR
    "bon": 1.5, "bien": 1.2, "bonne": 1.5, "excellent": 2.5, "excellente": 2.5,
    "genial": 2.2, "super": 2.0, "parfait": 2.5, "parfaite": 2.5,
    "satisfait": 1.8, "satisfaite": 1.8, "content": 1.5, "contente": 1.5,
    "heureux": 1.8, "heureuse": 1.8, "rapide": 1.5, "efficace": 1.6,
    "facile": 1.4, "pratique": 1.4, "fiable": 1.6, "agreable": 1.5,
    "magnifique": 2.2, "formidable": 2.2, "merci": 1.2, "top": 1.8,
    "cool": 1.4, "extraordinaire": 2.5, "recommande": 1.8, "recommander": 1.8,
    "adore": 2.0, "aime": 1.5, "meilleur": 1.8, "meilleure": 1.8,
    "impeccable": 2.2, "performant": 1.6, "performante": 1.6,
    "simple": 1.0, "rapidite": 1.4, "qualite": 1.2, "sympa": 1.4,
    "belle": 1.3, "beau": 1.3, "utile": 1.3, "stable": 1.2,
    "satisfaits": 1.8, "satisfaites": 1.8, "satisfaisant": 1.6,
    "satisfaisante": 1.6, "satisfaisants": 1.6, "satisfaisantes": 1.6,
    "satisfaire": 1.3, "fonctionne": 0.8, "marche": 0.8,
    # Anglais (données mixtes FR/EN)
    "good": 1.5, "great": 2.0, "excellent_en": 2.5, "amazing": 2.3,
    "awesome": 2.2, "love": 2.0, "best": 1.8, "nice": 1.3, "perfect": 2.5,
    "fast": 1.4, "easy": 1.3, "reliable": 1.5, "happy": 1.6, "satisfied": 1.6,
}

NEGATIVE_WORDS: dict[str, float] = {
    # Général FR
    "mauvais": -1.8, "mauvaise": -1.8, "nul": -2.0, "nulle": -2.0,
    "horrible": -2.5, "decevant": -1.8, "decevante": -1.8, "lent": -1.5,
    "lente": -1.5, "cher": -1.2, "chere": -1.2, "arnaque": -2.5,
    "probleme": -1.4, "problemes": -1.4, "panne": -1.6, "bug": -1.4,
    "bugs": -1.4, "erreur": -1.2, "impossible": -1.5, "incompetent": -2.0,
    "incompetente": -2.0, "catastrophe": -2.5, "honteux": -2.2,
    "honteuse": -2.2, "inadmissible": -2.2, "decu": -1.8, "decue": -1.8,
    "insatisfait": -1.8, "insatisfaite": -1.8, "difficile": -1.2,
    "complique": -1.2, "compliquee": -1.2, "injoignable": -1.8,
    "escroquerie": -2.5, "vol": -2.0, "voleur": -2.2, "voleurs": -2.2,
    "pire": -2.0, "deteste": -2.0, "detestable": -2.2, "inutile": -1.6,
    "insupportable": -2.2, "scandaleux": -2.3, "scandaleuse": -2.3,
    "coupure": -1.4, "coupures": -1.4, "bloque": -1.4, "bloquee": -1.4,
    "perdu": -1.3, "perdue": -1.3, "instable": -1.4, "medi": -1.0,
    "mediocre": -1.6, "regrette": -1.6, "fraude": -2.3,
    "defavorable": -1.8, "defavorables": -1.8, "obsolete": -1.6,
    "obsoletes": -1.6,
    # Anglais
    "bad": -1.5, "terrible": -2.3, "awful": -2.3, "worst": -2.3,
    "poor": -1.5, "disappointing": -1.8, "hate": -2.0, "scam": -2.5,
    "broken": -1.6, "useless": -1.8, "slow": -1.4, "fail": -1.6,
    "failed": -1.6, "annoying": -1.5,
}

# ---------------------------------------------------------------------------
# Lexique APPRIS sur le corpus
# ---------------------------------------------------------------------------

#: Poids découverts par `tools/build_lexicon.py` : log-odds entre les avis
#: 1-2 étoiles et les avis 4-5 étoiles, mesurés sur le corpus réel.
#:
#: POURQUOI IL EXISTE
#:     Le lexique écrit à la main plafonnait à 42,7 % d'exactitude — moins bien
#:     qu'un classifieur trivial répondant toujours « négatif » (52,4 %). Il
#:     avait été rédigé avec du vocabulaire télécom (panne, coupure, réseau)
#:     alors que les avis viennent de boutiques d'applications et parlent de
#:     l'app : « keeps crashing », « doesn't open », « unable to process ».
#:     95,9 % des avis 1-2 étoiles mal classés ne déclenchaient aucun mot.
#:     Avec ces poids : 73,4 %.
#:
#: LA NOTE N'EST JAMAIS LUE À L'EXÉCUTION. Elle n'a servi qu'à APPRENDRE ces
#: poids, hors ligne. Le classifieur reste purement textuel, comme exigé.
#:
#: Le fichier est OPTIONNEL : absent, le moteur retombe sur le seul lexique
#: écrit à la main. Le projet reste donc fonctionnel sans étape de génération.
_LEARNED_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "lexicon_learned.json"


def _load_learned() -> tuple[dict[str, float], float, float]:
    """Charge les poids appris et les seuils réglés avec eux."""
    try:
        data = json.loads(_LEARNED_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, 0.15, -0.15
    meta = data.get("_meta", {})
    return (
        data.get("poids", {}),
        float(meta.get("seuil_negatif", 0.15)),
        float(meta.get("seuil_positif", -0.15)),
    )


LEARNED_WEIGHTS, _SEUIL_NEG_BRUT, _SEUIL_POS_BRUT = _load_learned()

def _compound(raw_score: float) -> float:
    """Normalise un score brut vers [-1, 1], à la VADER."""
    alpha = 15.0
    return raw_score / (raw_score**2 + alpha) ** 0.5


# ---------------------------------------------------------------------------
# DEUX lexiques, pour DEUX domaines
# ---------------------------------------------------------------------------
#
# Les poids appris viennent EXCLUSIVEMENT d'avis d'applications mobiles. Les
# appliquer à des articles de presse est un abus de domaine, et il produit des
# absurdités vérifiées :
#
#   « La fibre optique ARRIVE dans certains quartiers »  -> négatif (« arrive »)
#   « Orange Tunisie lance Orange Satellite, SOLUTION… » -> négatif (« solution »)
#   « Test 5G : 500 Mbs en DOWNLOAD »                    -> négatif (« download »)
#
# Ces mots sont réellement discriminants chez les mécontents d'une application —
# « je n'arrive pas à », « le download échoue » — et ne veulent rien dire dans
# un article de journal. Résultat : la presse passait de 4 % à 27 % de négatifs,
# sans qu'aucune vérité terrain ne permette de le détecter, faute de note.
#
# Le lexique appris est donc réservé à son domaine d'origine. La presse retombe
# sur le lexique écrit à la main, qui est du vocabulaire général (« arnaque »,
# « panne », « excellent ») et reste valable hors du monde des applications.

#: Domaines reconnus. Repris tels quels de `dim_source.kind`, pour qu'il n'y ait
#: qu'un seul vocabulaire à connaître dans tout le projet.
CUSTOMER_REVIEW = "customer_review"
PRESS = "press"

#: Lexique écrit à la main : vocabulaire général, valable dans les deux domaines.
CURATED_WEIGHTS: dict[str, float] = {**NEGATIVE_WORDS, **POSITIVE_WORDS}

#: Lexique des avis clients : l'appris, complété du curé.
#:
#: Les mots ÉCRITS À LA MAIN l'emportent sur les mots appris : ils ont été
#: vérifiés, et leur poids porte une intention (« arnaque » à -2,5) qu'une
#: statistique ne doit pas écraser.
SENTIMENT_WEIGHTS: dict[str, float] = {**LEARNED_WEIGHTS, **CURATED_WEIGHTS}


#: Sources produisant de la PRESSE et non de la voix client.
#:
#: Liste explicite plutôt qu'un test sur une seule valeur : depuis l'ajout de
#: GDELT et des flux de presse spécialisée africaine, trois sources sur neuf
#: sont du journalisme. Les oublier ici les ferait juger au lexique des avis
#: clients, où « la fibre ARRIVE » devient une mauvaise nouvelle.
#:
#: REDDIT N'EN FAIT PAS PARTIE, et c'est délibéré : un fil de forum est de la
#: voix client, pas du journalisme. Il en a le registre — première personne,
#: familier, « my data is gone again » — que le lexique des avis clients sait
#: lire et celui de la presse interpréterait de travers.
PRESS_SOURCES = frozenset({"rss_feed", "gdelt", "press_feed"})


def domain_for_source(source) -> str:
    """Domaine d'un avis, déduit de sa source.

    Centraliser cette correspondance ici évite qu'un futur collecteur soit
    rattaché au mauvais lexique par inadvertance.
    """
    code = getattr(source, "value", source)
    return PRESS if code in PRESS_SOURCES else CUSTOMER_REVIEW

#: Seuils de décision, exprimés sur le score COMPOUND.
#:
#: `tools/build_lexicon.py` les règle sur la somme brute des poids ; le moteur,
#: lui, décide sur le compound normalisé. La conversion se fait ici, une fois,
#: avec la MÊME formule — recopier des valeurs à la main des deux côtés les
#: ferait diverger au premier réglage.
_SEUIL_NEG_COMPOUND = _compound(_SEUIL_NEG_BRUT)
_SEUIL_POS_COMPOUND = _compound(_SEUIL_POS_BRUT)

NEGATION_WORDS = {"ne", "n", "pas", "non", "aucun", "aucune", "jamais", "rien", "sans", "plus"}
INTENSIFIERS = {
    "tres": 1.3, "vraiment": 1.3, "trop": 1.25, "extremement": 1.6,
    "tellement": 1.35, "totalement": 1.3, "completement": 1.3,
    "super": 1.2, "hyper": 1.3, "very": 1.3, "really": 1.3, "so": 1.2,
}

_NEGATION_WINDOW = 3  # nb de tokens sur lesquels une négation agit

#: Préfixe des termes niés, tel qu'il est stocké ET affiché.
#:
#: Choisi pour se lire naturellement dans une liste de motifs — « pas satisfait »
#: se comprend d'un coup d'œil, là où un marqueur technique (« ¬satisfait »,
#: « NEG_satisfait ») demanderait une légende. Il fait partie de la clé de
#: regroupement : « lent » et « pas lent » sont deux motifs distincts, et les
#: confondre inverserait le sens de l'un des deux.
_NEGATED_PREFIX = "pas "

# Version du lexique, persistée avec chaque avis analysé (migration 004).
#
# À INCRÉMENTER à chaque modification de POSITIVE_WORDS, NEGATIVE_WORDS,
# NEGATION_WORDS ou INTENSIFIERS. C'est ce qui permet de savoir quelles lignes
# rejouer : sans ce numéro, on comparerait dans un même graphique des termes
# produits par deux lexiques différents, ou on ré-analyserait les 20 000 lignes
# à chaque déploiement.
#   tools/backfill_sentiment_terms.py ne retraite que les lignes dont la
#   lexicon_version est NULL ou inférieure à celle-ci.
#
#   v2 : les termes niés sont désormais préfixés (« pas satisfait »), afin que
#        l'onglet « Motifs » soit lisible sans note de bas de page.
#   v3 : lexique enrichi par apprentissage sur le corpus (tools/build_lexicon.py),
#        tokeniseur élargi à l'arabe. Exactitude 42,7 % -> 73,4 %.
#   v4 : mots-outils et NOMS DE MARQUE exclus. « orange » et « work » étaient
#        remontés en tête des motifs d'insatisfaction — statistiquement vrais,
#        vides de sens pour un lecteur métier. Coût assumé : 73,4 % -> 70,4 %
#        d'exactitude, contre un onglet Motifs redevenu lisible.
#   v5 : lexique CLOISONNÉ par domaine. Les poids appris sur des avis
#        d'applications ne s'appliquent plus à la presse, qui retombe sur le
#        lexique général — « la fibre ARRIVE » n'est plus une mauvaise nouvelle.
LEXICON_VERSION = 5


@dataclass
class SentimentScore:
    """Résultat d'une analyse de sentiment."""
    sentiment: SentimentEnum
    score: float  # compound score normalisé, entre -1 et 1
    positive_hits: int
    negative_hits: int
    # Termes du lexique effectivement déclenchés, dédoublonnés et sans accent
    # (forme comparable d'une source à l'autre). Persistés en base par la
    # migration 004 : ils alimentent l'onglet « Motifs d'insatisfaction » et,
    # plus tard, le contexte des agents IA — répondre « pourquoi le sentiment
    # baisse » demande les mots, pas le score.
    #
    # La polarité retenue est celle que le mot a PRISE dans la phrase, pas celle
    # qu'il porte au lexique, et le terme stocké porte la négation : dans
    # « pas rapide », c'est « pas rapide » qui atterrit dans negative_terms.
    # Classer sur le lexique produirait l'inverse du sens lu ; omettre le
    # préfixe afficherait « rapide » dans la liste des motifs d'insatisfaction.
    positive_terms: list[str] = field(default_factory=list)
    negative_terms: list[str] = field(default_factory=list)


def analyze_sentiment(
    text: Optional[str], domain: str = CUSTOMER_REVIEW
) -> SentimentScore:
    """
    Analyse le sentiment d'un texte via lexique pondéré.

    Args:
        text: Texte brut de l'avis (déjà normalisé par Review, ou brut)
        domain: CUSTOMER_REVIEW ou PRESS. Détermine QUEL lexique s'applique —
            voir le commentaire des deux lexiques plus haut. Un article de
            presse analysé avec le lexique des avis d'applications produit des
            classements absurdes ; c'est un abus de domaine, pas un réglage.

    Returns:
        SentimentScore avec le label et le score compound [-1, 1]
    """
    # Les seuils accompagnent le lexique : ceux du fichier appris ont été réglés
    # AVEC ces poids-là. Les appliquer au lexique curé décalerait les décisions.
    if domain == PRESS:
        poids, seuil_neg, seuil_pos = CURATED_WEIGHTS, -0.15, 0.15
    else:
        poids = SENTIMENT_WEIGHTS
        seuil_neg, seuil_pos = _SEUIL_NEG_COMPOUND, _SEUIL_POS_COMPOUND
    cleaned = clean_text(text or "")

    if not cleaned:
        return SentimentScore(SentimentEnum.NEUTRAL, 0.0, 0, 0)

    tokens = _tokenize(cleaned)

    raw_score = 0.0
    positive_hits = 0
    negative_hits = 0
    # dict et non set : les clés d'un dict conservent l'ordre d'insertion, donc
    # les termes ressortent dans l'ordre de lecture de l'avis (un set les
    # renverrait dans un ordre dépendant du hachage, donc instable d'un
    # processus à l'autre — insupportable pour des données persistées).
    positive_terms: dict[str, None] = {}
    negative_terms: dict[str, None] = {}
    pending_intensifier = 1.0
    last_boundary = -1  # index du dernier séparateur de clause (, . ; :)

    for i, tok in enumerate(tokens):
        if tok in _BOUNDARY_TOKENS:
            last_boundary = i
            continue
        if tok in ("!", "?"):
            continue  # gérées séparément (emphase globale)

        word = _strip_accents(tok)

        if word in INTENSIFIERS:
            pending_intensifier = INTENSIFIERS[word]
            continue

        weight = poids.get(word)
        if weight is None:
            pending_intensifier = 1.0
            continue

        # Négation : un mot de négation dans les _NEGATION_WINDOW tokens
        # précédents (sans franchir de virgule/point) inverse et atténue
        # le poids, comme dans VADER.
        window_start = max(last_boundary + 1, i - _NEGATION_WINDOW)
        negated = any(
            _strip_accents(t) in NEGATION_WORDS
            for t in tokens[window_start:i]
            if t not in _BOUNDARY_TOKENS
        )

        effective_weight = weight * pending_intensifier
        if negated:
            effective_weight *= -0.6

        raw_score += effective_weight
        # On enregistre `word` (sans accent, minuscule) et non `tok` : c'est la
        # forme qui sert de clé au lexique, donc la seule qui se regroupe
        # correctement entre sources qui n'accentuent pas de la même façon.
        #
        # Un mot NIÉ est préfixé. Sans ce préfixe, l'onglet « Motifs » affiche
        # « satisfait » ou « recommande » parmi les principaux motifs
        # d'insatisfaction d'une filiale : c'est exact — ces mots venaient de
        # « pas satisfait », « ne recommande pas » — mais illisible, et un
        # lecteur pressé en conclut que le classement est cassé. Le préfixe rend
        # le motif compréhensible sans note de bas de page, et regroupe
        # séparément « lent » et « pas lent », qui n'ont rien à voir.
        term = f"{_NEGATED_PREFIX}{word}" if negated else word
        if effective_weight > 0:
            positive_hits += 1
            positive_terms[term] = None
        elif effective_weight < 0:
            negative_hits += 1
            negative_terms[term] = None

        pending_intensifier = 1.0

    # Emphase par ponctuation (répétition de "!" ou "?")
    exclamations = cleaned.count("!")
    if exclamations:
        boost = min(exclamations, 4) * 0.3
        raw_score += boost if raw_score >= 0 else -boost

    compound = _compound(raw_score)

    # Attention au SIGNE : dans ce moteur un poids NÉGATIF marque le
    # mécontentement, donc un compound négatif = avis négatif. Les seuils
    # viennent du fichier appris, où ils ont été réglés sur le jeu
    # d'apprentissage avec ces mêmes poids — les reprendre à la main les
    # désaccorderait du lexique.
    if compound <= seuil_neg:
        sentiment = SentimentEnum.NEGATIVE
    elif compound >= seuil_pos:
        sentiment = SentimentEnum.POSITIVE
    else:
        sentiment = SentimentEnum.NEUTRAL

    return SentimentScore(
        sentiment=sentiment,
        score=round(compound, 4),
        positive_hits=positive_hits,
        negative_hits=negative_hits,
        positive_terms=list(positive_terms),
        negative_terms=list(negative_terms),
    )
