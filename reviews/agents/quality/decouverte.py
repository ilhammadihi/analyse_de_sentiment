"""
Module 3 — chercher des sources non exploitées, sans jamais en inventer.

LA CONTRAINTE RÉELLE, ÉNONCÉE SANS DÉTOUR
    L'énoncé demande d'« utiliser les outils de recherche/web déjà disponibles
    dans l'architecture ». Il n'y en a AUCUN : vérifié, rien dans
    `requirements.txt` ni dans le code n'interroge un moteur de recherche.

    En même temps, la section 22 interdit d'inventer une source, et la
    section 7 de considérer une source comme fiable au seul motif qu'elle
    apparaît quelque part.

    Un modèle à qui l'on demanderait « quelles plateformes d'avis existent aux
    Comores ? » produirait des URL plausibles et fausses. C'est la seule chose
    qu'il puisse faire sans accès au web, et c'est précisément ce qui est
    proscrit.

CE QUI EST FAIT À LA PLACE, ET POURQUOI C'EST PLUS SOLIDE
    Deux gisements DÉCLARÉS, puis une sonde HTTP RÉELLE.

      1. `config/regulators.json` — 54 régulateurs nationaux, déjà sondés et
         datés par `tools/verify_regulators.py`, dont 40 en état OK. Ce fichier
         existait pour trancher l'existence d'un opérateur ; il sert ici de
         source candidate officielle. C'est l'espèce de preuve la plus forte de
         tout le dispositif (voir `claims.evaluer_corroboration`), et le
         périmètre n'en avait aucune.

      2. `config/source_catalog.json` — plateformes candidates déclarées à la
         main, versionnées, chacune motivée.

    Puis CHAQUE candidate est interrogée en HTTP avant d'être proposée. C'est
    la sonde qui décide, pas le catalogue : une URL qui répond 404 est REJETÉE,
    une URL qui répond 403 est marquée bloquée et proposée avec sa réserve.

    Le résultat est donc TRAÇABLE À DEUX NIVEAUX : une ligne de configuration
    versionnée, et un code HTTP daté. C'est plus vérifiable qu'un résultat de
    moteur de recherche, que personne ne pourrait reproduire six mois plus tard.

C'EST EXACTEMENT LA MÉTHODE DE `tools/probe_gap_operators.py`
    « Une application vivante, éditée par l'opérateur, avec des avis sur la
    boutique de son pays, est un opérateur qui existe. » Ici : une page qui
    répond et porte du vocabulaire télécom est une source exploitable. Le
    raisonnement est déjà éprouvé dans ce dépôt.

AJOUTER UN VRAI MOTEUR RESTE POSSIBLE SANS RIEN CASSER
    `Sonde` est une interface. Le jour où une clé de recherche existe, il
    suffit d'ajouter un fournisseur de candidates à côté des deux gisements
    déclarés : la sonde, le format de sortie et les garde-fous ne bougent pas.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

import requests

from reviews.agents.quality.couverture import CouvertureFiliale

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
CATALOG_PATH = _CONFIG_DIR / "source_catalog.json"
REGULATORS_PATH = _CONFIG_DIR / "regulators.json"

#: Vocabulaire attendu dans une page pour qu'elle soit jugée pertinente.
#:
#: Multilingue, comme `press_relevance` : le périmètre couvre le français,
#: l'anglais, le portugais et l'arabe. Une page qui répond 200 mais ne parle
#: pas de télécoms n'est pas une source — c'est une page de garage ou une
#: redirection publicitaire.
_VOCABULAIRE = (
    "telecom", "télécom", "mobile", "réseau", "reseau", "network",
    "opérateur", "operateur", "operator", "internet", "sms", "4g", "5g",
    "abonné", "abonne", "subscriber", "forfait", "recharge", "roaming",
)

#: Motif de lecture d'un volume d'avis affiché SUR LA PAGE elle-même.
#:
#: BEST-EFFORT, ET C'EST UNE FRONTIÈRE VOULUE. Il s'agit de LIRE un nombre déjà
#: écrit par la page sondée (« 86 avis », « 1 204 reviews »), jamais de
#: recalculer un volume en listant les fiches — cela ferait de la sonde un
#: collecteur, exactement ce que la section 8 de l'énoncé interdit.
#:
#: Ce nombre est ce qui rend une proposition ACTIONNABLE pour une équipe qui
#: doit arbitrer si un connecteur vaut la peine d'être écrit : « source
#: candidate » sans volume est une piste, avec un volume c'est un ordre de
#: grandeur sur lequel décider.
_MOTIF_VOLUME = re.compile(
    r"([\d][\d\s .,]{0,9})\s*(?:avis|reviews?|évaluations?|evaluations?|"
    r"notes?|comments?)",
    re.IGNORECASE,
)

#: Volume au-delà duquel une correspondance est écartée comme non plausible.
#:
#: Une page peut contenir « 2024 reviews of the year » ou un identifiant
#: numérique sans rapport : au-delà de ce plancher, mieux vaut ne rien
#: afficher qu'afficher un nombre fantaisiste — même règle que partout dans ce
#: module, ne jamais inventer une donnée.
_VOLUME_PLAFOND = 500_000


def _lire_volume_avis(texte: str) -> Optional[int]:
    """Cherche un nombre d'avis annoncé dans le texte de la page, ou None.

    Prend la plus grande correspondance plausible plutôt que la première : une
    page liste souvent d'abord un compteur de navigation à un chiffre (« 4.5
    ★ ») avant le vrai total. Rend None sans hésiter dès qu'aucun motif fiable
    n'apparaît — un champ vide vaut mieux qu'un nombre inventé.
    """
    candidats: list[int] = []
    for brut in _MOTIF_VOLUME.findall(texte):
        nettoye = re.sub(r"[\s .,]", "", brut)
        if not nettoye.isdigit():
            continue
        valeur = int(nettoye)
        if 0 < valeur <= _VOLUME_PLAFOND:
            candidats.append(valeur)
    return max(candidats) if candidats else None


class Sonde(Protocol):
    """Interrogation réelle d'une URL. Remplaçable en test."""

    def __call__(self, url: str) -> dict[str, Any]:  # pragma: no cover - protocole
        ...


@dataclass
class Candidate:
    """Une source candidate, telle qu'elle sera proposée et tracée."""

    source_name: str
    url: str
    country: Optional[str] = None
    operator: Optional[str] = None
    subsidiary_id: Optional[int] = None
    subsidiary: Optional[str] = None
    source_type: str = "inconnu"
    accessibility: str = "inconnu"
    estimated_relevance: str = "medium"
    reason: str = ""
    apport: str = ""
    connector_required: bool = True
    status: str = "CANDIDATE"
    confidence: float = 0.0
    probe_status: Optional[int] = None
    probe_at: Optional[datetime] = None
    #: Volume d'avis LU sur la page sondée, jamais recompté. Voir
    #: `_lire_volume_avis`. None si aucun motif fiable n'a été trouvé.
    avis_estimes: Optional[int] = None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Format exact demandé par la section 7 de l'énoncé, enrichi de la sonde."""
        return {
            "source_name": self.source_name,
            "url": self.url,
            "country": self.country or "",
            "operator": self.operator or "",
            "subsidiary": self.subsidiary or "",
            "estimated_relevance": self.estimated_relevance,
            "reason": self.reason,
            "source_type": self.source_type,
            "accessibility": self.accessibility,
            "evidence": self.evidence,
            # Au-delà du format demandé : ce qui rend la proposition
            # actionnable plutôt que simplement lisible.
            "status": self.status,
            "confidence": round(self.confidence, 2),
            "connector_required": self.connector_required,
            "apport": self.apport,
            "probe_status": self.probe_status,
            "avis_estimes": self.avis_estimes,
        }


def sonde_http(url: str, timeout: int = 15) -> dict[str, Any]:
    """Interroge réellement une URL et rapporte ce qu'elle a répondu.

    NE LÈVE JAMAIS. Une candidate injoignable est une information — « bloqué »,
    « absent » — et non une panne de l'agent. Faire remonter l'exception ferait
    échouer tout le passage sur une seule URL morte.

    On lit un extrait du corps pour vérifier le VOCABULAIRE : un code 200 ne
    prouve rien à lui seul, beaucoup d'hébergeurs rendent 200 sur une page de
    parking. C'est le même contrôle que celui de `verify_regulators.py`.
    """
    depart = datetime.now(timezone.utc)
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            # Un en-tête de navigateur : plusieurs plateformes refusent les
            # clients sans user-agent, et on mesurerait alors un blocage qui
            # n'existe pas pour un vrai connecteur.
            headers={"User-Agent": "Mozilla/5.0 (compatible; qualite-donnees/1.0)"},
        )
    except requests.RequestException as exc:
        return {
            "http": None,
            "accessibility": "injoignable",
            "vocabulaire": False,
            "erreur": str(exc)[:200],
            "url_finale": url,
            "le": depart.isoformat(),
        }

    texte = ""
    try:
        texte = (resp.text or "")[:20000].lower()
    except Exception:  # noqa: BLE001
        # Contenu non textuel (PDF, image) : la page existe mais n'est pas
        # exploitable telle quelle. On ne le traite pas comme une erreur.
        texte = ""

    if resp.status_code == 404:
        acces = "absent"
    elif resp.status_code in (401, 403, 429):
        acces = "bloque"
    elif resp.status_code >= 500:
        acces = "injoignable"
    elif resp.status_code < 400:
        acces = "http_ouvert"
    else:
        acces = "inconnu"

    return {
        "http": resp.status_code,
        "accessibility": acces,
        "vocabulaire": any(mot in texte for mot in _VOCABULAIRE),
        # Lu ici, sur le MÊME texte déjà téléchargé pour le contrôle de
        # vocabulaire — aucune requête supplémentaire, donc aucun pas de plus
        # vers un comportement de collecteur.
        "avis_estimes": _lire_volume_avis(texte),
        "url_finale": str(resp.url),
        "le": depart.isoformat(),
    }


class DecouverteSources:
    """Propose des sources candidates pour une filiale réellement sous-couverte."""

    def __init__(
        self,
        *,
        sonde: Optional[Sonde] = None,
        probe_enabled: bool = True,
        max_candidates: int = 3,
        timeout: int = 15,
    ):
        self.sonde = sonde or (lambda url: sonde_http(url, timeout=timeout))
        self.probe_enabled = probe_enabled
        self.max_candidates = max_candidates
        self._catalogue: Optional[list[dict]] = None
        self._regulateurs: Optional[dict[str, dict]] = None

    # ------------------------------------------------------------------ Public

    def pour(self, couverture: CouvertureFiliale) -> list[Candidate]:
        """Candidates pour une filiale, les plus sûres d'abord.

        N'EST APPELÉE QUE SUR UN DIAGNOSTIC ENRICHISSABLE. Le garde-fou est
        chez l'appelant (`guardian`), et c'est voulu : ce module ne doit pas
        pouvoir décider tout seul qu'il est temps de chercher ailleurs — ce
        serait contourner l'ordre imposé par la section 5 de l'énoncé.
        """
        candidates = self._depuis_regulateur(couverture) + self._depuis_catalogue(
            couverture
        )
        if not candidates:
            return []

        retenues: list[Candidate] = []
        for candidate in candidates:
            self._instruire(candidate)
            if candidate.status != "REJECTED":
                retenues.append(candidate)
            if len(retenues) >= self.max_candidates:
                break

        retenues.sort(key=lambda c: -c.confidence)
        return retenues

    # ------------------------------------------------------------- Gisements

    def _depuis_regulateur(self, c: CouvertureFiliale) -> list[Candidate]:
        """Le régulateur national du pays, s'il a été sondé avec succès.

        PLACÉ EN PREMIER, et ce n'est pas un détail de tri : une source
        officielle est la seule espèce de preuve capable de faire passer une
        affirmation en `CONFIRMED`. Aucune n'existe aujourd'hui dans le corpus,
        et c'est le manque le plus structurant du module de corroboration.
        """
        reg = self._charger_regulateurs().get((c.iso2 or "").upper())
        if not reg or not reg.get("site"):
            return []

        # On ne propose PAS un régulateur dont la sonde de `verify_regulators`
        # a échoué : le fichier porte déjà ce verdict, daté. Le contredire ici
        # reviendrait à proposer une piste qu'on sait morte.
        etat = (reg.get("sonde") or {}).get("etat") or ""
        if not etat.startswith("OK"):
            return []

        return [
            Candidate(
                source_name=f"{reg.get('sigle') or 'Régulateur'} — {reg.get('nom') or ''}".strip(" —"),
                url=reg.get("page_operateurs") or reg["site"],
                country=c.iso2,
                operator=c.operator,
                subsidiary_id=c.subsidiary_id,
                subsidiary=c.subsidiary,
                source_type="regulateur",
                estimated_relevance="high",
                reason=(
                    f"Régulateur national des télécommunications de {c.country}. "
                    "Publie décisions, sanctions et indicateurs de qualité de "
                    "service — la seule espèce de preuve OFFICIELLE dont le "
                    "corpus est aujourd'hui totalement dépourvu."
                ),
                apport=(
                    "Ne fournit pas d'avis clients, mais permet de CONFIRMER une "
                    "affirmation (panne, hausse tarifaire) au lieu de la laisser "
                    "au statut non corroboré."
                ),
                evidence=[
                    {
                        "type": "declaration_verifiee",
                        "source": "config/regulators.json",
                        "fait": "régulateur sondé avec succès",
                        "http": (reg.get("sonde") or {}).get("http"),
                        "date": reg.get("verifie_le"),
                    }
                ],
            )
        ]

    def _depuis_catalogue(self, c: CouvertureFiliale) -> list[Candidate]:
        """Plateformes déclarées couvrant ce pays."""
        out: list[Candidate] = []
        operateur = c.operator or ""
        for entree in self._charger_catalogue():
            pays = entree.get("pays") or []
            if "*" not in pays and (c.iso2 or "").upper() not in pays:
                continue
            url = str(entree.get("url_gabarit") or "").format(
                operateur=_pour_url(operateur), pays=_pour_url(c.country or "")
            )
            if not url:
                continue
            out.append(
                Candidate(
                    source_name=entree.get("nom") or "?",
                    url=url,
                    country=c.iso2,
                    operator=operateur,
                    subsidiary_id=c.subsidiary_id,
                    subsidiary=c.subsidiary,
                    source_type=entree.get("type") or "inconnu",
                    estimated_relevance="medium",
                    reason=entree.get("raison") or "",
                    apport=entree.get("apport") or "",
                    evidence=[
                        {
                            "type": "declaration",
                            "source": "config/source_catalog.json",
                            "fait": "plateforme candidate déclarée",
                            "reserve": entree.get("reserve"),
                        }
                    ],
                )
            )
        return out

    # ------------------------------------------------------------ Instruction

    def _instruire(self, candidate: Candidate) -> None:
        """Sonde la candidate et en tire son statut, son accès et sa confiance.

        SANS SONDE, LA CANDIDATE RESTE « CANDIDATE » ET SA CONFIANCE BASSE.
        Elle n'est jamais présentée comme vérifiée : c'est la différence entre
        une piste et un fait, et l'énoncé exige qu'elle soit visible.
        """
        if not self.probe_enabled:
            candidate.confidence = 0.3
            candidate.evidence.append(
                {
                    "type": "sonde",
                    "source": "aucune",
                    "fait": "sonde désactivée : la candidate n'est pas vérifiée",
                }
            )
            return

        try:
            resultat = self.sonde(candidate.url)
        except Exception:  # noqa: BLE001
            logger.warning("Sonde en échec sur %s", candidate.url, exc_info=True)
            candidate.confidence = 0.2
            return

        candidate.probe_status = resultat.get("http")
        candidate.accessibility = resultat.get("accessibility") or "inconnu"
        candidate.probe_at = datetime.now(timezone.utc)
        candidate.evidence.append(
            {
                "type": "sonde",
                "source": "requête HTTP",
                "fait": "réponse mesurée de l'URL candidate",
                "http": resultat.get("http"),
                "accessibility": candidate.accessibility,
                "vocabulaire_telecom": resultat.get("vocabulaire"),
                "url_finale": resultat.get("url_finale"),
                "date": resultat.get("le"),
            }
        )

        # Une URL absente est REJETÉE, définitivement. Proposer une piste qu'on
        # sait morte fait perdre le crédit de toutes les autres.
        if candidate.accessibility == "absent":
            candidate.status = "REJECTED"
            candidate.confidence = 0.0
            return

        if candidate.accessibility == "http_ouvert":
            if resultat.get("vocabulaire"):
                # Répond ET parle de télécoms : c'est le seul cas où l'on
                # affirme quelque chose. Deux faits mesurés, pas un.
                candidate.status = "VERIFIED"
                candidate.confidence = 0.8
                candidate.estimated_relevance = "high"
                # Le volume n'est retenu QUE sur une candidate déjà vérifiée :
                # un chiffre lu sur une page de parking ou une redirection
                # n'aurait aucun sens à afficher, même s'il matche le motif.
                candidate.avis_estimes = resultat.get("avis_estimes")
            else:
                # Répond mais ne parle pas du sujet : page de parking,
                # redirection, ou gabarit de recherche mal formé. On ne rejette
                # pas — la page peut charger son contenu en JavaScript — mais on
                # ne la crédite pas non plus.
                candidate.status = "CANDIDATE"
                candidate.confidence = 0.4
                candidate.estimated_relevance = "low"
            return

        if candidate.accessibility == "bloque":
            # Un blocage est une INFORMATION UTILE, pas un échec : c'est ce qui
            # a fait écarter Techpoint Africa et MyBroadband des flux de presse.
            # La source existe probablement ; elle exigera un navigateur.
            candidate.status = "CANDIDATE"
            candidate.confidence = 0.45
            candidate.connector_required = True
            candidate.apport += (
                " Accès refusé à un client HTTP simple : l'intégration "
                "demanderait un navigateur, comme Google Maps."
            )
            return

        candidate.status = "CANDIDATE"
        candidate.confidence = 0.25

    # ------------------------------------------------------------- Chargement

    def _charger_catalogue(self) -> list[dict]:
        if self._catalogue is None:
            self._catalogue = _lire_json(CATALOG_PATH, "plateformes") or []
        return self._catalogue

    def _charger_regulateurs(self) -> dict[str, dict]:
        if self._regulateurs is None:
            lignes = _lire_json(REGULATORS_PATH, "regulateurs") or []
            self._regulateurs = {
                (r.get("iso2") or "").upper(): r for r in lignes if r.get("iso2")
            }
        return self._regulateurs


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _lire_json(chemin: Path, cle: str) -> Optional[list]:
    """Lit une liste d'un fichier de configuration. Absence tolérée.

    Un catalogue manquant dégrade la découverte, il ne casse rien : l'agent
    continue de diagnostiquer et de scorer. Même principe que `load_cities()`,
    dont l'absence fait retomber Google Maps au niveau pays sans erreur.
    """
    if not chemin.exists():
        logger.warning("%s absent : découverte de sources dégradée.", chemin.name)
        return None
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("%s illisible : %s", chemin.name, exc)
        return None
    return data.get(cle) if isinstance(data, dict) else None


def _pour_url(valeur: str) -> str:
    """Rend un nom d'opérateur utilisable dans une URL de recherche."""
    from urllib.parse import quote_plus

    return quote_plus((valeur or "").strip())
