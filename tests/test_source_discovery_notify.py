"""
Ajustement métier (17 août 2026) : découverte de source → volume lu, message
court, jamais répété.

CE QUE CES TESTS PROTÈGENT
    Trois fautes possibles, aucune ne lève d'exception :

      - un nombre INVENTÉ dans le message Telegram — le motif de lecture du
        volume est un heuristique, pas une mesure certaine, et un chiffre
        fantaisiste décrédibiliserait toute annonce future ;
      - une source déjà annoncée qui repart un jour dans le message suivant —
        exactement ce que « évite les alertes répétitives » interdit ;
      - un message qui redevient technique (code HTTP, score, jargon) — le
        métier a été explicite sur ce point, et c'est la limite la plus facile
        à laisser filer par accident lors d'un futur ajustement.

AUCUN RÉSEAU, AUCUNE BASE RÉELLE. Les doubles rendent ce qu'on leur donne.
"""

from contextlib import contextmanager

import pytest

from reviews.agents.quality.decouverte import Candidate, _lire_volume_avis


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Curseur:
    def __init__(self, lignes=None):
        self.lignes = list(lignes or [])
        self.sql = None
        self.params = None
        self.executions = []

    def execute(self, sql, params=None):
        self.sql, self.params = sql, list(params or [])
        self.executions.append((sql, self.params))

    def executemany(self, sql, lignes):
        self.sql, self.params = sql, list(lignes)
        self.executions.append((sql, list(lignes)))

    def fetchall(self):
        return self.lignes

    def fetchone(self):
        return self.lignes[0] if self.lignes else None


class _Base:
    def __init__(self, lignes=None):
        self.curseur = _Curseur(lignes)

    def cursor(self, dict_rows: bool = False):
        @contextmanager
        def _ouvrir():
            yield self.curseur

        return _ouvrir()


class _Canal:
    def __init__(self, ok=True):
        self.ok = ok
        self.envois: list[str] = []

    def send_text(self, corps):
        self.envois.append(corps)
        return self.ok


# ===========================================================================
# 1. Lecture du volume — un heuristique, jamais une mesure certaine
# ===========================================================================


def test_lit_un_volume_annonce_par_la_page():
    assert _lire_volume_avis("cette page recense 86 avis vérifiés") == 86


def test_prend_le_plus_grand_motif_plausible():
    """Une page liste souvent d'abord un compteur de navigation à un chiffre
    avant le vrai total. Prendre le premier motif rencontré sous-estimerait
    systématiquement le volume réel."""
    texte = "note moyenne sur 5 avis affichés · 1204 reviews au total"
    assert _lire_volume_avis(texte) == 1204


def test_un_nombre_invraisemblable_est_ecarte():
    """« 2024 reviews of the year » ou un identifiant sans rapport ne doit
    jamais devenir un volume affiché : mieux vaut rien qu'un chiffre inventé."""
    assert _lire_volume_avis("999999999 reviews") is None


def test_sans_motif_reconnu_rend_none_et_pas_zero():
    """None doit rester distinct de 0 : une page qui n'affiche pas de compteur
    n'a pas « zéro avis », elle n'a simplement rien à lire."""
    assert _lire_volume_avis("bienvenue sur notre site") is None


def test_le_volume_n_est_retenu_que_sur_une_candidate_verifiee():
    """Un chiffre lu sur une page de parking ou un blocage n'a aucun sens à
    afficher, même s'il matche le motif par accident."""
    from reviews.agents.quality.decouverte import DecouverteSources

    d = DecouverteSources(
        sonde=lambda url: {
            "http": 403, "accessibility": "bloque", "vocabulaire": False,
            "avis_estimes": 500,
        }
    )
    candidate = Candidate(source_name="X", url="https://bloque.example")
    d._instruire(candidate)

    assert candidate.status == "CANDIDATE"
    assert candidate.avis_estimes is None


def test_une_candidate_verifiee_recupere_son_volume_lu():
    from reviews.agents.quality.decouverte import DecouverteSources

    d = DecouverteSources(
        sonde=lambda url: {
            "http": 200, "accessibility": "http_ouvert", "vocabulaire": True,
            "texte": "86 avis sur mtn", "avis_estimes": 86, "url_finale": url,
        }
    )
    candidate = Candidate(source_name="X", url="https://ok.example", operator="MTN")
    d._instruire(candidate)

    assert candidate.status == "VERIFIED"
    assert candidate.avis_estimes == 86
    assert candidate.as_dict()["avis_estimes"] == 86


def test_un_nom_d_operateur_non_reconnaissable_ne_verifie_jamais_par_accident():
    """RÉGRESSION ÉVITÉE À LA SOURCE : « WE » (Telecom Egypt) est un mot
    anglais courant — voir `reviews.collectors.targets._NOMS_NON_RECONNAISSABLES`
    et son cas mesuré (« We need better broadband in Lagos » -> WE Égypte).
    Une page générique qui parle de télécoms et contient, par pur hasard, le
    pronom « we » ne doit jamais suffire à vérifier cette candidate : ce
    serait exactement le faux positif que le contrôle du nom d'opérateur a
    été ajouté pour éliminer, reproduit pour l'unique opérateur que ce dépôt
    a déjà mesuré comme non reconnaissable en texte libre."""
    from reviews.agents.quality.decouverte import DecouverteSources

    d = DecouverteSources(
        sonde=lambda url: {
            "http": 200, "accessibility": "http_ouvert", "vocabulaire": True,
            "texte": "we offer the best mobile network and customer service",
            "avis_estimes": None, "url_finale": url,
        }
    )
    candidate = Candidate(source_name="X", url="https://generique.example", operator="WE")
    d._instruire(candidate)

    assert candidate.status != "VERIFIED"


# ===========================================================================
# 2. Notification — courte, actionnable, jamais deux fois
# ===========================================================================


def _guardian(canal=None, lignes_db=None):
    from reviews.agents.quality.guardian import QualityGuardian
    from reviews.config import get_settings

    return QualityGuardian(_Base(lignes_db), get_settings(), notifier=canal)


def test_le_message_est_court_et_sans_jargon_technique():
    """LA LIMITE LA PLUS FACILE À PERDRE DE VUE. Le détail complet vit dans
    l'onglet Data Quality ; ce message n'est qu'un signal suivi d'une
    proposition — jamais un code HTTP, un score ou un mot de diagnostic."""
    canal = _Canal()
    agent = _guardian(
        canal,
        [
            {
                "candidate_id": 1, "source_name": "ComplaintsBoard",
                "url": "https://complaintsboard.com/x", "source_type": "plateforme_avis",
                "avis_estimes": 86, "accessibility": "http_ouvert",
                "subsidiary": "Comores Telecom",
            }
        ],
    )
    n = agent._notifier_nouvelles_sources()

    assert n == 1
    (message,) = canal.envois
    assert "Comores Telecom" in message
    assert "ComplaintsBoard" in message
    assert "86 avis visibles" in message
    assert "Source publique et gratuite" in message
    assert "➡️" in message
    for interdit in ("HTTP", "http_ouvert", "confidence", "score", "VERIFIED", "%"):
        assert interdit not in message, f"« {interdit} » ne doit pas apparaître"
    # Court au sens du gabarit métier : un en-tête, la source, le volume,
    # l'accès, la proposition — huit lignes espacées, pas un rapport.
    assert len(message.splitlines()) <= 9


def test_omet_le_volume_plutot_que_d_afficher_un_zero_invente():
    canal = _Canal()
    agent = _guardian(
        canal,
        [
            {
                "candidate_id": 1, "source_name": "X", "url": "u",
                "source_type": "forum", "avis_estimes": None,
                "accessibility": "http_ouvert", "subsidiary": "Orange Mali",
            }
        ],
    )
    agent._notifier_nouvelles_sources()

    (message,) = canal.envois
    assert "avis visibles" not in message
    assert "0 avis" not in message


def test_une_source_deja_annoncee_ne_repart_jamais():
    """La dédoublonnage se fait EN BASE (`notified_at IS NULL`), et ce test
    vérifie que la requête l'exprime bien — c'est la seule protection contre
    une répétition, et elle doit être dans le SQL, pas dans la mémoire du
    processus."""
    from reviews.storage.quality_repository import QualityRepository

    base = _Base([])
    QualityRepository(base).candidates_a_notifier()

    assert "notified_at IS NULL" in base.curseur.sql
    assert "status = 'VERIFIED'" in base.curseur.sql


def test_marquer_notifiees_ecrit_notified_at():
    from reviews.storage.quality_repository import QualityRepository

    base = _Base()
    QualityRepository(base).marquer_notifiees([1, 2, 3])

    assert "notified_at = now()" in base.curseur.sql
    assert base.curseur.params == [[1, 2, 3]]


def test_enregistrer_candidates_ne_touche_jamais_notified_at():
    """Sinon la sonde, rejouée à chaque passage, effacerait le souvenir d'une
    annonce déjà faite et la source repartirait dans le message suivant."""
    from reviews.storage.quality_repository import QualityRepository

    base = _Base()
    QualityRepository(base).enregistrer_candidates(
        [{"source_name": "X", "url": "u", "status": "VERIFIED"}]
    )
    apres_conflit = base.curseur.sql.split("DO UPDATE")[1]

    assert "notified_at       =" not in apres_conflit
    assert "notified_at = EXCLUDED" not in apres_conflit


def test_seul_le_statut_verified_declenche_une_annonce():
    """Une simple `CANDIDATE` — sondée mais pas confirmée — n'est pas une
    découverte, c'est une piste. L'annoncer enverrait chercher une source dont
    on ne sait pas si elle contient quoi que ce soit."""
    from reviews.storage.quality_repository import QualityRepository

    base = _Base([])
    QualityRepository(base).candidates_a_notifier()
    assert "'VERIFIED'" in base.curseur.sql


def test_n_est_marquee_que_ce_qui_a_reellement_ete_envoye():
    """Un canal injoignable ne doit pas faire perdre l'annonce pour de bon :
    la marquer quand même la ferait disparaître silencieusement de la file,
    et personne ne serait jamais prévenu de cette découverte."""
    canal = _Canal(ok=False)
    agent = _guardian(
        canal,
        [
            {"candidate_id": 9, "source_name": "X", "url": "u",
             "source_type": "forum", "avis_estimes": None,
             "accessibility": "http_ouvert", "subsidiary": "Y"},
        ],
    )
    n = agent._notifier_nouvelles_sources()

    assert n == 0
    # Rien ne doit avoir été marqué : aucun UPDATE sur source_candidates.
    marquages = [
        sql for sql, _ in agent.depot.db.curseur.executions
        if "notified_at = now()" in sql
    ]
    assert marquages == []


def test_sans_canal_configure_rien_n_est_tente():
    agent = _guardian(canal=None, lignes_db=[{"candidate_id": 1}])
    assert agent._notifier_nouvelles_sources() == 0


def test_une_base_illisible_ne_leve_pas():
    from reviews.agents.quality.guardian import QualityGuardian
    from reviews.config import get_settings

    class _BaseQuiLeve:
        def cursor(self, dict_rows: bool = False):
            raise RuntimeError("indisponible")

    agent = QualityGuardian(_BaseQuiLeve(), get_settings(), notifier=_Canal())
    assert agent._notifier_nouvelles_sources() == 0  # ne lève pas


def test_l_appel_est_plafonne_a_la_retenue_configuree():
    """Même retenue que pour les filiales : un lot de dix découvertes d'un
    coup n'est pas plus lisible qu'un lot de dix alertes."""
    from reviews.storage.quality_repository import QualityRepository

    base = _Base([])
    QualityRepository(base).candidates_a_notifier(limit=3)

    assert base.curseur.params[-1] == 3
