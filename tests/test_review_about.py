"""
Séparation des avis sur l'APPLICATION et des avis sur la FILIALE (migration 019).

CE QUE CES TESTS PROTÈGENT, ET POURQUOI AUCUN AUTRE NE LE FERAIT
    Les fautes visées ici ne provoquent pas d'erreur. Elles produisent des
    chiffres faux sous une API qui répond 200 :

      - un aspect ajouté à la taxonomie Python et jamais classé dans
        `dim_aspect` : ses avis retombent dans la satisfaction du service par
        présomption de source, silencieusement ;
      - un prédicat d'objet écrit en égalité plutôt qu'en exclusion : les 2 006
        avis qui nomment les deux griefs disparaissent des DEUX côtés ;
      - le défaut du filtre remis à « les deux » : on retrouve les fausses
        alertes de pic que la séparation existe pour supprimer.
"""

import pytest

from reviews.domain.aspects import (
    APP_ASPECTS,
    ASPECTS,
    OPERATOR_ASPECTS,
    OTHER,
    VALID_ASPECTS,
    scope,
)
from reviews.storage.filters import (
    ALERTS,
    ALLOWED_ABOUT,
    APP,
    ASPECTS as ASPECT_COLUMNS,
    BOTH_SIDES,
    ENRICHED,
    OPERATOR,
    TERMS,
    StatsFilter,
)


# ---------------------------------------------------------------------------
# Découpage de la taxonomie
# ---------------------------------------------------------------------------


def test_chaque_aspect_tombe_d_un_cote_ou_est_le_repli():
    """Aucun aspect ne doit rester non classé.

    C'est LE test de la migration 019. Un aspect oublié n'est pas rejeté : il
    n'entre dans aucune des deux listes, donc l'avis qui le porte est classé sur
    la présomption de sa source au lieu de l'être sur ce qu'il dit. Un grief
    applicatif remonté par un nouvel aspect se remettrait alors à peser sur la
    satisfaction du service, sans rien pour le signaler.
    """
    non_classes = VALID_ASPECTS - APP_ASPECTS - OPERATOR_ASPECTS - {OTHER}
    assert non_classes == set(), (
        f"Aspects sans côté : {sorted(non_classes)}. Les déclarer dans "
        "APP_ASPECTS (reviews/domain/aspects.py) ou les laisser tomber par "
        "soustraction dans OPERATOR_ASPECTS, et ajouter la ligne correspondante "
        "à dim_aspect (nouvelle migration)."
    )


def test_les_deux_cotes_sont_disjoints():
    assert APP_ASPECTS & OPERATOR_ASPECTS == set()


def test_le_repli_ne_tranche_aucun_cote():
    """« autre » ne doit faire basculer aucun avis.

    La moitié du corpus n'exprime aucun motif nommable (« good », « smooth »,
    « très bien »). Si `OTHER` comptait comme un aspect d'opérateur, ces avis
    seraient tous rangés du côté du service — y compris les 16 000 notes
    d'application à deux mots, qui sont précisément ce que la séparation écarte.
    """
    assert OTHER not in APP_ASPECTS
    assert OTHER not in OPERATOR_ASPECTS
    assert scope(OTHER) == "none"


def test_scope_classe_les_aspects_connus():
    assert scope("app_bugs") == "app"
    assert scope("reseau_couverture") == "operator"
    assert scope("aspect_invente") == "none"


def test_les_aspects_applicatifs_existent_dans_la_taxonomie():
    """Garde-fou contre une faute de frappe dans APP_ASPECTS.

    Un `app_bug` au lieu d'`app_bugs` ne lèverait rien : l'aspect n'existerait
    simplement dans aucune liste, et les 3 598 avis de bugs repartiraient du
    côté opérateur.
    """
    assert APP_ASPECTS <= set(ASPECTS)


# ---------------------------------------------------------------------------
# Accord entre la taxonomie Python et son miroir SQL
# ---------------------------------------------------------------------------


def test_le_miroir_sql_declare_les_memes_cotes():
    """`dim_aspect` (migration 019) doit dire la même chose que ce module.

    Le SQL ne peut pas importer la taxonomie : les vues lisent `dim_aspect`. Les
    deux listes sont donc tenues à deux endroits — la situation que ce projet
    évite partout ailleurs, et qu'il faut donc surveiller ici.

    Le test lit le fichier de migration plutôt que la base : il doit échouer sur
    un poste sans PostgreSQL, au moment où la divergence est INTRODUITE, pas
    après un déploiement.
    """
    import re
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1] / "migrations" / "019_review_about.sql"
    ).read_text(encoding="utf-8")

    bloc = migration.split("INSERT INTO dim_aspect", 1)[1].split(";", 1)[0]
    declares = {
        aspect: cote
        for aspect, cote in re.findall(r"\('([a-z_]+)',\s*'(app|operator|none)'\)", bloc)
    }

    attendus = {aspect: scope(aspect) for aspect in VALID_ASPECTS}
    assert declares == attendus, (
        "dim_aspect et reviews/domain/aspects.py ont divergé. Écarts : "
        f"{sorted(set(attendus.items()) ^ set(declares.items()))}"
    )


# ---------------------------------------------------------------------------
# Axe de filtre
# ---------------------------------------------------------------------------


def test_le_defaut_est_le_service_de_la_filiale():
    """Le défaut porte toute la correction.

    Un tableau de bord qui compare des filiales compare des SERVICES. Remettre
    ce défaut à « les deux » suffirait à faire revenir les 20 107 notes
    d'application dans les taux de mécontentement, donc les fausses alertes de
    pic, donc les diagnostics de l'Agent 1 sur un réseau qui n'a pas bougé.
    """
    assert StatsFilter().about == OPERATOR


def test_le_predicat_exclut_l_autre_cote_au_lieu_d_exiger_le_sien():
    """`<> 'app'` et non `= 'operator'`, sinon les avis mixtes disparaissent.

    2 006 avis nomment un grief applicatif ET un grief de service, et affichent
    93 % de négatifs : ce sont les plus argumentés du corpus. Une égalité les
    ferait tomber des deux côtés à la fois.
    """
    sql, params = StatsFilter(days=30).where()
    assert "v.about <> %s" in sql
    assert APP in params

    sql, params = StatsFilter(days=30, about=APP).where()
    assert "v.about <> %s" in sql
    assert OPERATOR in params


def test_les_deux_cotes_confondus_ne_posent_aucun_predicat():
    sql, _ = StatsFilter(days=30, about=BOTH_SIDES).where()
    assert "about" not in sql


def test_l_objet_n_est_jamais_interpole_dans_le_sql():
    """La valeur passe par un paramètre lié, comme tout le reste du module."""
    sql, params = StatsFilter(days=30, about=APP).where()
    assert "'operator'" not in sql
    assert "'app'" not in sql
    assert OPERATOR in params


@pytest.mark.parametrize("cols", [ENRICHED, TERMS, ASPECT_COLUMNS])
def test_toutes_les_vues_filtrables_portent_l_axe(cols):
    """L'invariant du module : un axe manquant est un filtre ignoré en silence.

    Si v_review_aspects n'exposait pas `about`, l'onglet Motifs continuerait
    d'agréger les bugs d'application pendant que la vue d'ensemble les écarte —
    deux écrans, deux chiffres, aucune erreur.
    """
    sql, _ = StatsFilter(days=30).where(cols=cols)
    assert f"{cols.about} <> %s" in sql
    assert cols.about != "NULL"


@pytest.mark.parametrize("cols", [ENRICHED, TERMS, ASPECT_COLUMNS])
def test_toutes_les_vues_filtrables_portent_la_preuve(cols):
    """Idem pour `about_source`, sur lequel repose le mode citation.

    Une colonne absente y serait pire qu'un filtre ignoré : le prédicat est une
    ÉGALITÉ, donc `NULL = 'aspects'` — toujours faux. La liste de verbatims se
    viderait sans cause visible, exactement la panne que `for_alerts()` avait
    dû corriger sur `source_kind`.
    """
    sql, _ = StatsFilter(days=30, about_strict=True).where(cols=cols)
    assert f"{cols.about_source} = %s" in sql
    assert cols.about_source != "NULL"


def test_les_alertes_neutralisent_l_axe():
    """La table `alerts` n'a pas d'objet : le prédicat produirait `NULL <> 'app'`.

    Toujours faux, donc un fil d'alertes qui se vide sans cause visible — la
    panne exacte que `for_alerts()` a été écrit pour empêcher sur `source_kind`.
    """
    f = StatsFilter(days=30, about=APP).for_alerts()
    assert f.about == BOTH_SIDES
    sql, _ = f.where(cols=ALERTS)
    assert "NULL <>" not in sql


def test_le_seuil_de_fiabilite_compte_du_cote_regarde():
    """Le décompte doit compter comme comptent les taux (invariant de la 007).

    Sinon une filiale entre dans un classement d'applications grâce à des avis
    de service — avec un dénominateur bien plus petit que celui annoncé.
    """
    sql, _ = StatsFilter(days=30, min_subsidiary_reviews=30).where()
    assert "avis_clients >= %s" in sql

    sql, _ = StatsFilter(days=30, about=APP, min_subsidiary_reviews=30).where()
    assert "avis_app >= %s" in sql


def test_citer_est_exclusif_la_ou_compter_est_inclusif():
    """Les deux modes doivent produire deux prédicats OPPOSÉS.

    Mesuré sur les trois pics du 16 août : les avis cités en preuve étaient
    tous des avis mixtes au texte dominé par l'application (« App is typically
    good... however the app is currently not starting up at all ») sous un
    titre annonçant une dégradation du SERVICE. Le taux était juste, la
    citation le démentait — et c'est la citation que le lecteur retient.
    """
    compter, p_compter = StatsFilter(days=30).where()
    citer, p_citer = StatsFilter(days=30, about_strict=True).where()

    assert "v.about <> %s" in compter and APP in p_compter
    assert "v.about = %s" in citer and OPERATOR in p_citer


def test_citer_exige_la_preuve_et_pas_seulement_la_purete():
    """Un avis classé sur le seul défaut de sa source n'est jamais citable.

    Mesuré au 16 août : 1 911 des 10 093 avis du côté opérateur sont classés par
    PRÉSOMPTION (Google Maps 1 872), et deux d'entre eux sont de purs avis
    d'application — « In any case, this app is great! ». Deux lignes sur dix
    mille, mais une citation est ce que le lecteur retient : une seule sous un
    « pic de mécontentement » discrédite l'alerte entière.

    L'exigence porte sur la CITATION seulement. Étendue aux taux, elle
    retirerait les 1 872 notes d'agences — le socle du signal des 130 filiales
    que Google Maps couvre seul — pour rattraper ces deux lignes.
    """
    citer, p_citer = StatsFilter(days=30, about_strict=True).where()
    assert "v.about_source = %s" in citer
    assert "aspects" in p_citer

    compter, p_compter = StatsFilter(days=30).where()
    assert "about_source" not in compter
    assert "aspects" not in p_compter


def test_le_mode_strict_suit_le_cote_demande():
    _, params = StatsFilter(days=30, about=APP, about_strict=True).where()
    assert APP in params
    assert OPERATOR not in params


def test_le_mode_strict_ne_s_applique_pas_aux_deux_cotes_confondus():
    sql, _ = StatsFilter(days=30, about=BOTH_SIDES, about_strict=True).where()
    assert "about" not in sql


# ---------------------------------------------------------------------------
# Côté de l'ASPECT — distinct du côté de l'AVIS
# ---------------------------------------------------------------------------


def test_les_aspects_contredisant_le_cote_sont_ecartes():
    """`about` porte sur l'AVIS, `aspect_scope` sur l'ASPECT.

    Un avis mixte reste dans le périmètre du service — il contient bien une
    plainte de service. Mais son aspect applicatif n'a rien à faire dans un
    classement de motifs de service : mesuré après la migration 019, « Bugs de
    l'application » y arrivait quatrième avec 1 030 avis, et l'Agent 1 le
    recopiait dans un briefing annonçant une dégradation du service.
    """
    clause, params = StatsFilter(days=30).aspect_scope_clause()
    assert "t.aspect_scope <> %s" in clause
    assert params == [APP]

    clause, params = StatsFilter(days=30, about=APP).aspect_scope_clause()
    assert params == [OPERATOR]


def test_aucun_aspect_n_est_ecarte_quand_les_deux_cotes_sont_demandes():
    clause, params = StatsFilter(days=30, about=BOTH_SIDES).aspect_scope_clause()
    assert clause == ""
    assert params == []


def test_le_cote_de_l_aspect_ne_se_confond_pas_avec_celui_de_l_avis():
    """Les deux prédicats visent des colonnes DIFFÉRENTES.

    Les confondre écarterait l'avis mixte du périmètre au lieu de n'écarter que
    son aspect applicatif — soit 2 006 avis, les plus argumentés du corpus,
    retirés du taux du service qu'ils décrivent pourtant.
    """
    where, _ = StatsFilter(days=30).where(cols=ASPECT_COLUMNS)
    clause, _ = StatsFilter(days=30).aspect_scope_clause()
    assert "t.about" in where
    assert "t.about" not in clause


def test_l_objet_accompagne_chaque_reponse():
    """Un taux lu sans savoir sur quoi il porte est un taux mal interprété."""
    assert StatsFilter(days=30, about=APP).describe()["about"] == APP


def test_la_liste_blanche_couvre_exactement_les_valeurs_utilisees():
    assert ALLOWED_ABOUT == {OPERATOR, APP, BOTH_SIDES}
