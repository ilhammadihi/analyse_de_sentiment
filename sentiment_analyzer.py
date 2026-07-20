"""
Analyse de sentiment basée sur un lexique (français + anglais).
Approche légère type VADER : lexique pondéré, négation, intensificateurs,
ponctuation. Aucune dépendance externe, aucun modèle à télécharger.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from models import SentimentEnum

# ---------------------------------------------------------------------------
# Nettoyage / préparation du texte
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_REPEATED_CHAR_RE = re.compile(r"(.)\1{2,}")  # "supeeeer" -> "supeer"
_TOKEN_RE = re.compile(r"[a-zà-öø-ÿ']+|[!?]+|[,.;:]", re.IGNORECASE)
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

NEGATION_WORDS = {"ne", "n", "pas", "non", "aucun", "aucune", "jamais", "rien", "sans", "plus"}
INTENSIFIERS = {
    "tres": 1.3, "vraiment": 1.3, "trop": 1.25, "extremement": 1.6,
    "tellement": 1.35, "totalement": 1.3, "completement": 1.3,
    "super": 1.2, "hyper": 1.3, "very": 1.3, "really": 1.3, "so": 1.2,
}

_NEGATION_WINDOW = 3  # nb de tokens sur lesquels une négation agit


@dataclass
class SentimentScore:
    """Résultat d'une analyse de sentiment."""
    sentiment: SentimentEnum
    score: float  # compound score normalisé, entre -1 et 1
    positive_hits: int
    negative_hits: int


def _compound(raw_score: float) -> float:
    """Normalise un score brut vers [-1, 1], à la VADER."""
    alpha = 15.0
    return raw_score / (raw_score**2 + alpha) ** 0.5


def analyze_sentiment(text: Optional[str]) -> SentimentScore:
    """
    Analyse le sentiment d'un texte d'avis via lexique pondéré FR/EN.

    Args:
        text: Texte brut de l'avis (déjà normalisé par Review, ou brut)

    Returns:
        SentimentScore avec le label et le score compound [-1, 1]
    """
    cleaned = clean_text(text or "")

    if not cleaned:
        return SentimentScore(SentimentEnum.NEUTRAL, 0.0, 0, 0)

    tokens = _tokenize(cleaned)

    raw_score = 0.0
    positive_hits = 0
    negative_hits = 0
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

        weight = POSITIVE_WORDS.get(word) or NEGATIVE_WORDS.get(word)
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
        if effective_weight > 0:
            positive_hits += 1
        elif effective_weight < 0:
            negative_hits += 1

        pending_intensifier = 1.0

    # Emphase par ponctuation (répétition de "!" ou "?")
    exclamations = cleaned.count("!")
    if exclamations:
        boost = min(exclamations, 4) * 0.3
        raw_score += boost if raw_score >= 0 else -boost

    compound = _compound(raw_score)

    if compound >= 0.15:
        sentiment = SentimentEnum.POSITIVE
    elif compound <= -0.15:
        sentiment = SentimentEnum.NEGATIVE
    else:
        sentiment = SentimentEnum.NEUTRAL

    return SentimentScore(
        sentiment=sentiment,
        score=round(compound, 4),
        positive_hits=positive_hits,
        negative_hits=negative_hits,
    )
