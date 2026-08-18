"""
Tests de la sélection des preuves de presse.

Ce qui est éprouvé ici, c'est le CHOIX des articles proposés comme cause
possible d'une dégradation. Une erreur y est particulièrement coûteuse : elle
ne lève rien, ne vide aucun écran, et se contente de faire dire au système une
absurdité crédible — « la satisfaction chute, peut-être à cause du lancement de
la 5G ». Personne ne signale ce genre de défaut, on cesse simplement de lire le
bloc.
"""

from datetime import date

from reviews.storage.press_repository import PressRepository


class _CurseurEspion:
    """Retient la requête et ses paramètres, rend des lignes fixes."""

    def __init__(self, lignes=None):
        self.sql = None
        self.params = None
        self.lignes = lignes or []

    def execute(self, sql, params=None):
        self.sql, self.params = sql, list(params or [])

    def fetchall(self):
        return self.lignes


class _Db:
    def __init__(self, lignes=None):
        self.curseur = _CurseurEspion(lignes)

    def cursor(self, dict_rows=False):
        from contextlib import contextmanager

        @contextmanager
        def _ouvrir():
            yield self.curseur

        return _ouvrir()


def _ligne(titre, sentiment="neutral", jour=8):
    return {
        "title": titre,
        # Vocabulaire télécom obligatoire : `est_pertinent` filtre en Python
        # après la requête, un titre sans lexique métier serait écarté.
        "text": "réseau mobile opérateur forfait",
        "date_article": date(2026, 8, jour),
        "media": "Presse",
        "subsidiary": "MTN Ghana",
        "country": "Ghana",
        "sentiment": sentiment,
    }


def _appel(lignes):
    db = _Db(lignes)
    resultat = PressRepository(db).evidence(
        window=(date(2026, 8, 1), date(2026, 8, 15)),
        level="country",
        value="GH",
        limit=5,
    )
    return resultat, db.curseur


# ---------------------------------------------------------------------------
# Tonalité : la bonne nouvelle n'explique pas la mauvaise
# ---------------------------------------------------------------------------


def test_les_articles_positifs_sont_ecartes_par_la_requete():
    """RÉGRESSION VÉCUE : la sélection se faisait sur la seule date, et
    proposait « inwi renforce la couverture mobile » ou « Chinguitel lance la
    5G » comme cause possible d'une chute de satisfaction.

    Une amélioration annoncée n'explique pas un mécontentement. Le filtre est
    posé en SQL et non après coup : écarter en Python ferait remonter des
    lignes pour les jeter, et la limite serait consommée par des articles
    qu'on ne montrera jamais.
    """
    _, curseur = _appel([])
    assert "<> 'positive'" in curseur.sql


def test_le_neutre_est_conserve():
    """Une décision de régulateur ou une hausse tarifaire est rédigée sans
    affect et sort NEUTRE du lexique — or c'est exactement le genre
    d'événement recherché. Mesuré, le corpus visible est neutre à 87,6 % :
    ne garder que le négatif réduirait la couverture à presque rien."""
    resultat, _ = _appel([_ligne("Hausse des tarifs data au Ghana", "neutral")])
    assert len(resultat["articles"]) == 1
    assert resultat["articles"][0]["tonalite"] == "neutral"


def test_le_negatif_passe_devant_la_fraicheur():
    """Entre une panne et un communiqué anodin du même jour, la panne est le
    meilleur candidat : la tonalité est le PREMIER critère de tri, la date le
    second. Inverser les deux ferait remonter le communiqué le plus récent.
    """
    _, curseur = _appel([])
    sql = " ".join(curseur.sql.split())

    ordre = sql[sql.index("ORDER BY"):]
    position_tonalite = ordre.index("negative")
    position_date = ordre.index("created_at")
    assert position_tonalite < position_date, (
        "la fraîcheur prime sur la tonalité : un communiqué neutre récent "
        "passerait devant une panne de la veille"
    )


def test_la_tonalite_est_transmise_a_l_appelant():
    """Le modèle doit pouvoir écrire « une panne rapportée le 4 août » plutôt
    que de traiter un communiqué neutre avec la même force."""
    resultat, _ = _appel([_ligne("Panne majeure du réseau", "negative")])
    assert resultat["articles"][0]["tonalite"] == "negative"


# ---------------------------------------------------------------------------
# Élargissement au pays : sans l'actualité des concurrents
# ---------------------------------------------------------------------------


def test_l_elargissement_au_pays_exclut_les_articles_des_concurrents():
    """RÉGRESSION MESURÉE SUR LES VRAIES ALERTES.

    Faute de presse propre à la filiale, la recherche s'élargit au pays — et
    remontait alors l'actualité des CONCURRENTS : « MTN Nigeria's growth engine
    stalled » proposé sous une alerte Glo Nigeria, un article sur Vodafone sous
    e& Égypte, un article sur Vodacom sous Telkom South Africa.

    Les résultats d'un concurrent n'expliquent pas le mécontentement d'un
    opérateur — ils le contredisent même parfois. À la maille pays on ne garde
    donc que les articles NON RATTACHÉS (régulateur, panne nationale, hausse
    tarifaire) et ceux qui portent sur cette filiale précise.
    """
    db = _Db([])

    class _CurseurParents(_CurseurEspion):
        def fetchone(self):
            return (7, "NG")  # operator_id, iso2

    db.curseur = _CurseurParents([])
    PressRepository(db).evidence(
        window=(date(2026, 8, 1), date(2026, 8, 15)),
        level="subsidiary",
        value="42",
        limit=5,
    )
    sql = " ".join(db.curseur.sql.split())

    assert "iso2 = %s" in sql, "l'élargissement au pays n'a pas eu lieu"
    assert "subsidiary_id IS NULL" in sql, "les articles nationaux sont perdus"
    assert "subsidiary_id = %s" in sql, "les articles de la filiale sont perdus"
    # La filiale analysée doit figurer dans les paramètres de la clause pays.
    assert 42 in db.curseur.params


# ---------------------------------------------------------------------------
# Garde-fous conservés
# ---------------------------------------------------------------------------


def test_un_article_hors_sujet_reste_ecarte_apres_la_requete():
    """Le filtre de pertinence télécom s'applique EN PLUS de la tonalité : un
    article négatif sur un barrage n'explique pas une chute de satisfaction
    télécom."""
    hors_sujet = {
        "title": "Le barrage de Knysna atteint 60 % de sa capacité",
        "text": "eau pluie réservoir agriculture",
        "date_article": date(2026, 8, 8),
        "media": "Presse", "subsidiary": None, "country": "Ghana",
        "sentiment": "negative",
    }
    resultat, _ = _appel([hors_sujet])
    assert resultat["articles"] == []


def test_les_reprises_du_meme_evenement_sont_dedoublonnees():
    """Google News rend le même fait repris par plusieurs médias. Sans
    dédoublonnage, les places disponibles seraient occupées par un seul
    événement, et le lecteur y verrait une insistance là où il n'y a qu'une
    reprise."""
    doublons = [_ligne("Panne du réseau national", "negative", jour=j) for j in (8, 7, 6)]
    resultat, _ = _appel(doublons)
    assert len(resultat["articles"]) == 1
