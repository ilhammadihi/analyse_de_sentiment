"""
Modèles de données du domaine (Pydantic v2).
Purs : aucune dépendance I/O (ni BD, ni réseau). Testables isolément.
"""

from typing import Optional
from datetime import datetime
from enum import Enum
import hashlib

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator, computed_field


class SentimentEnum(str, Enum):
    """Sentiments possibles."""
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class SourceEnum(str, Enum):
    """Sources possibles.

    Toute valeur ajoutée ici doit l'être AUSSI dans `dim_source` (migration),
    sinon l'avis s'insère mais reste orphelin des dimensions : il disparaît de
    toutes les vues du dashboard, sans erreur pour le signaler.
    """
    TRUSTPILOT = "trustpilot"
    GOOGLE_PLAY = "google_play"
    APP_STORE = "app_store"
    GOOGLE_MAPS = "google_maps"
    RSS_FEED = "rss_feed"
    HELLOPETER = "hellopeter"
    GDELT = "gdelt"
    PRESS_FEED = "press_feed"
    REDDIT = "reddit"


class Review(BaseModel):
    """Un avis collecté, validé avant insertion en base."""

    model_config = ConfigDict(use_enum_values=True)

    id: str = Field(..., min_length=1, description="ID unique de l'avis")
    company: str = Field(..., min_length=1, description="Nom de l'entreprise")
    source: SourceEnum = Field(..., description="Source de collecte")
    title: Optional[str] = Field(None, max_length=500, description="Titre de l'avis")
    text: str = Field(..., min_length=1, max_length=5000, description="Texte de l'avis")
    rating: Optional[int] = Field(None, ge=1, le=5, description="Note de 1 à 5")
    sentiment: Optional[SentimentEnum] = Field(None, description="Sentiment détecté")
    created_at: Optional[datetime] = Field(default_factory=datetime.utcnow, description="Date de création")
    author: Optional[str] = Field(None, max_length=255, description="Auteur de l'avis")
    likes: Optional[int] = Field(None, ge=0, description="Nombre de likes")
    verified: Optional[bool] = Field(None, description="Achat vérifié")

    # --- Point de vente d'origine (migration 008) ---------------------------
    # Renseignés par le seul collecteur Google Maps : les six autres sources
    # n'ont aucune notion de lieu. Un avis d'application n'a pas d'agence.
    target_id: Optional[str] = Field(
        None, max_length=255,
        description="Identifiant de la sous-cible (agence, application)"
    )
    target_name: Optional[str] = Field(
        None, max_length=255, description="Nom lisible de la sous-cible"
    )

    # --- Sortie détaillée du moteur de sentiment (migration 004) -------------
    # Renseignés par le pipeline lors de l'enrichissement, jamais par les
    # collecteurs. Le label `sentiment` seul dit QUE ça va mal ; ces trois
    # champs disent COMBIEN (score continu) et POURQUOI (termes déclenchés).
    # Ils n'entrent pas dans le checksum de déduplication : ce sont des données
    # dérivées du texte, pas du contenu collecté.
    sentiment_score: Optional[float] = Field(
        None, ge=-1, le=1, description="Score compound du sentiment, sur [-1, 1]"
    )
    pos_terms: list[str] = Field(
        default_factory=list, description="Termes positifs déclenchés"
    )
    neg_terms: list[str] = Field(
        default_factory=list, description="Termes négatifs déclenchés"
    )
    lexicon_version: Optional[int] = Field(
        None, description="Version du lexique ayant produit score et termes"
    )

    @field_validator("text", mode="before")
    @classmethod
    def normalize_text(cls, v):
        """Normalise le texte (whitespace)."""
        if not v:
            return v
        return " ".join(str(v).split()).strip()

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, v):
        """Normalise le titre."""
        if not v:
            return None
        return str(v).strip()

    @model_validator(mode="after")
    def compute_sentiment_from_rating(self):
        """Sentiment de repli déduit de la note si non fourni.

        Le pipeline recalcule ensuite le sentiment à partir du texte (NLP) ;
        cette règle ne sert que de valeur par défaut cohérente.
        """
        if self.sentiment is None and self.rating:
            if self.rating >= 4:
                self.sentiment = SentimentEnum.POSITIVE.value
            elif self.rating == 3:
                self.sentiment = SentimentEnum.NEUTRAL.value
            else:
                self.sentiment = SentimentEnum.NEGATIVE.value
        return self

    def get_checksum(self) -> str:
        """Hash SHA256 du contenu (déduplication).

        Le LIEU entre dans le hash quand il est connu, et seulement alors.

        Depuis que Google Maps visite plusieurs agences par filiale, deux
        clients de deux boutiques différentes écrivant « Bon service » ne sont
        plus le même avis. Sans le lieu dans le hash, le second serait écarté
        comme doublon : plus on couvre d'agences, plus on perdrait d'avis
        courts, et la densification se saborderait elle-même.

        Le lieu est AJOUTÉ, jamais inséré au milieu. Une clé de la forme
        `entreprise:source::texte` — avec un séparateur vide pour les sources
        sans lieu — changerait le hash de TOUTES les lignes existantes, qui
        seraient alors réinsérées en masse comme si elles étaient neuves.
        """
        content = f"{self.company}:{self.source}:{self.text}"
        if self.target_id:
            content = f"{content}:{self.target_id}"
        return hashlib.sha256(content.encode()).hexdigest()


class ScraperResult(BaseModel):
    """Résultat de collecte + traitement d'une source."""

    scraper_name: str
    reviews: list[Review] = Field(default_factory=list)
    inserted_count: int = 0
    duplicate_count: int = 0
    error_count: int = 0
    started_at: datetime
    ended_at: Optional[datetime] = None
    error_message: Optional[str] = None
    status: str = "pending"  # pending, running, success, failed

    @computed_field
    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class PipelineRun(BaseModel):
    """Une exécution complète du pipeline."""

    run_id: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    status: str = "running"  # running, success, failed
    total_reviews: int = 0
    total_duplicates: int = 0
    total_errors: int = 0
    error_message: Optional[str] = None
    scraper_results: dict[str, ScraperResult] = Field(default_factory=dict)

    #: Durée au-delà de laquelle CE run est anormalement long, en secondes.
    #:
    #: Un seuil unique ne peut pas convenir : depuis que chaque collecteur a son
    #: propre planificateur, un run porte une seule source, et les sources n'ont
    #: rien de comparable. Google Maps met une dizaine d'heures pour une cadence
    #: de vingt-quatre — il est dans son budget ; un flux RSS qui met dix
    #: minutes pour une cadence de six heures ne l'est pas moins, mais le même
    #: chiffre absolu classerait le premier en panne et laisserait passer le
    #: second. Le budget retenu est la CADENCE de la source : la dépasser
    #: signifie qu'elle ne rattrapera jamais son retard, ce qui est la seule
    #: définition utile de « trop lent ».
    #:
    #: `None` = pas de budget connu (exécution manuelle) ; la règle retombe
    #: alors sur son seuil historique.
    budget_seconds: Optional[float] = None

    @computed_field
    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class Alert(BaseModel):
    """Une alerte métier détectée pendant un run."""

    type: str                       # ex: negative_spike, zero_reviews, run_failed
    severity: AlertSeverity
    title: str
    message: str
    run_id: Optional[str] = None
    company: Optional[str] = None
    source: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    #: Avis qui ont provoqué l'alerte, deux ou trois au plus.
    #:
    #: POURQUOI CE CHAMP EXISTE. « 67 % d'avis négatifs sur 12 avis » dit qu'il
    #: se passe quelque chose, jamais QUOI. Le destinataire devait ouvrir le
    #: dashboard, retrouver la filiale et lire les avis pour savoir s'il
    #: s'agissait d'une panne réseau ou d'un litige de facturation — trois
    #: gestes avant de pouvoir décider quoi que ce soit.
    #:
    #: NON PERSISTÉ, à dessein : ces extraits vivent le temps de la
    #: notification. Le dashboard, lui, sait déjà afficher les avis d'une
    #: filiale et le fait mieux — les recopier dans la table `alerts` les
    #: figerait et les dupliquerait sans rien apporter.
    evidence: list[str] = Field(default_factory=list)

    #: Événements extérieurs datés de la fenêtre du pic — articles de presse.
    #:
    #: DISTINCT DE `evidence`, ET CE N'EST PAS UNE NUANCE. Les avis disent ce
    #: que les clients RESSENTENT ; un article de presse dit ce qui s'est
    #: PASSÉ. Confondus dans une même liste, le lecteur prendrait une décision
    #: de régulateur pour une plainte d'abonné.
    #:
    #: Un article contemporain n'est jamais une cause démontrée — il coïncide.
    #: L'écran doit le présenter comme tel, jamais comme l'explication.
    events: list[str] = Field(default_factory=list)

    #: Maille à laquelle les événements ont été trouvés, en français.
    #:
    #: Faute de presse propre à une filiale, la recherche s'élargit au pays.
    #: « Un article national du 31 juillet » et « un article sur cette
    #: filiale » n'ont pas la même valeur, et rien dans le titre ne permet de
    #: les distinguer. Champ à part plutôt que première ligne d'`events` : ce
    #: n'est pas un événement, et l'afficher comme tel en ferait une puce parmi
    #: les autres.
    events_scope: Optional[str] = None
