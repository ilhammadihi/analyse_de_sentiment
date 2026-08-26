"""
Fusionne `config/discovered_apps.json` dans `config/operators.json`.

La découverte ratisse large — c'est son rôle. La fusion, elle, tranche : c'est
ici que s'appliquent les règles qui décident si une application peut être
attribuée à une filiale sans fausser ses chiffres.

TROIS RÈGLES, chacune née d'un faux positif OBSERVÉ dans les résultats bruts.

1. L'ÉDITEUR DOIT ÊTRE L'OPÉRATEUR, ou l'application doit nommer le pays.
   La découverte a remonté pour Cell C des applications éditées par « Evolve
   VAS » et « Cellfind PTY LTD », et pour MTN Afrique du Sud des outils de
   « Seamless Distibution » et « iCrypto, Inc. ». Ces éditeurs publient des
   applications de revendeur ou d'assistance portant la marque, mais leurs avis
   ne parlent pas du service de l'opérateur.

   La règle n'exige pas l'éditeur SEUL, parce que ce serait faux : Orange
   Sénégal publie sous « SONATEL S.A. », sa raison sociale, qui ne contient pas
   « Orange ». D'où l'alternative : éditeur à la marque OU nom d'application
   désignant explicitement le pays.

2. AUCUNE APPLICATION NE DOIT NOMMER UN AUTRE PAYS DU PÉRIMÈTRE.
   Observé : « MyMTN Congo » remonté pour MTN Afrique du Sud. Le contrôle de
   pays de la découverte ne l'avait pas vu, ses indices exigeant « congo
   kinshasa » ou « congo brazzaville » et non « congo » seul. La fusion
   re-contrôle avec la table complète des 54 pays (`collectors/countries.py`),
   noms français ET anglais.

3. UN PAQUET GOOGLE PLAY N'APPARTIENT QU'À UNE SEULE FILIALE.
   Mesuré et déjà consigné dans les tests : interrogé avec `country=tz`, `zm`,
   `ng`, `ke` puis `ug`, le paquet `com.airtel.africa.selfcare` renvoie
   exactement LES MÊMES vingt avis. Google Play ne segmente pas ses avis par
   pays. Partager un paquet entre filiales dupliquerait donc les mêmes avis sur
   autant de marchés, et attribuerait à chaque pays le sentiment de tous les
   autres — précisément ce que le tableau de bord sert à distinguer.

   L'App Store, lui, segmente bien par vitrine nationale : partager un
   `app_id` y est légitime tant que `store_country` diffère.

Usage :
    python -m tools.merge_apps            # écrit operators.json
    python -m tools.merge_apps --dry-run  # montre sans écrire
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reviews.collectors.countries import COUNTRIES  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "operators.json"
DISCOVERED_PATH = Path(__file__).resolve().parent.parent / "config" / "discovered_apps.json"

#: Applications retenues par filiale et par boutique. Au-delà, on descend dans
#: la longue traîne des outils internes (« MTN EVD », « SIM Tracking ») dont les
#: avis ne parlent pas du service au client.
MAX_APPS_PAR_BOUTIQUE = 6


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _mot(marque: str, texte: str) -> bool:
    """La marque apparaît-elle comme un MOT entier dans le texte ?"""
    if not marque:
        return False
    return bool(re.search(rf"\b{re.escape(marque)}\b", texte))


#: Marqueurs de pays HORS périmètre africain, tels qu'ils apparaissent dans les
#: noms d'applications télécoms.
#:
#: `nomme_un_autre_pays` ne connaît que les 54 pays suivis, ce qui suffisait
#: tant que les opérateurs du périmètre n'exerçaient qu'en Afrique. e& (ex-
#: Etisalat) est présent aux Émirats et en Afghanistan, et la fusion a retenu
#: pour sa filiale ÉGYPTIENNE les applications « e& UAE », « e& money UAE » et
#: « My e& AFG ». Leurs avis sont émiratis et afghans : les compter comme
#: égyptiens fausse exactement ce que le tableau de bord compare.
#
#: Les marchés européens de Vodafone posent le même problème : « My Vodafone
#: Ireland », « My Vodafone (GR) », « MeinVodafone » et « Můj Vodafone+ » ont
#: été retenus pour Vodafone ÉGYPTE, l'éditeur étant bien « Vodafone ».
#:
#: Cette liste est énumérative, donc incomplète par construction : elle couvre
#: les marchés hors Afrique où les opérateurs du périmètre exercent réellement.
#: Une application d'un marché non listé peut encore passer — d'où le contrôle
#: humain qui reste nécessaire sur les groupes multinationaux.
MARQUEURS_HORS_PERIMETRE = (
    "uae", "afg", "afghanistan", "emirates", "ksa", "saudi", "qatar", "oman",
    "bahrain", "kuwait", "jordan", "lebanon", "pakistan", "india", "turkey",
    "france", "espana", "portugal", "romania", "moldova", "png",
    "ireland", "irlande", "greece", "grece", "albania", "albanie",
    "germany", "deutschland", "mein", "czech", "cesko", "muj", "hungary",
    "italia", "italy", "spain", "netherlands", "nederland", "srilanka",
    "nepal", "iraq", "yemen", "syria", "bangladesh", "myanmar",
)


#: Suffixe de marché entre parenthèses — « My Vodafone (GR) », « (AL) », « (CZ) ».
#: Les codes à deux lettres ne peuvent pas être cherchés comme des mots isolés
#: (« al » apparaît dans des translittérations arabes), mais entre parenthèses
#: ils sont sans ambiguïté : c'est la convention de nommage des groupes
#: multinationaux pour distinguer leurs déclinaisons nationales.
SUFFIXE_MARCHE = re.compile(
    r"\((gr|al|ie|de|cz|hu|ro|it|es|nl|pt|tr|in|pk|uk|au|nz|qa|ae)\)", re.I)

#: Concaténations sans séparateur, qu'aucune recherche par mot ne peut trouver.
COLLES_HORS_PERIMETRE = ("meinvodafone", "mujvodafone", "mivodafone")


def nomme_hors_perimetre(texte: str) -> str | None:
    """Marqueur de pays hors Afrique trouvé dans le libellé, sinon None."""
    if SUFFIXE_MARCHE.search(texte):
        return SUFFIXE_MARCHE.search(texte).group(1).lower()
    compact = norm(texte).replace(" ", "")
    colle = next((c for c in COLLES_HORS_PERIMETRE if c in compact), None)
    if colle:
        return colle
    hay = norm(texte)
    return next((m for m in MARQUEURS_HORS_PERIMETRE if _mot(m, hay)), None)


def marque_concurrente(texte: str, sub: dict, marques: set[str]) -> str | None:
    """Marque d'un AUTRE opérateur du périmètre citée dans le libellé.

    Née d'un faux positif observé : « My Orange Egypt » (app_id 942568333) a été
    attribuée à la filiale égyptienne d'e&. Elle avait passé la règle « le nom
    désigne le pays » — ce qui est vrai, il nomme bien l'Égypte — sans que rien
    ne vérifie de QUEL opérateur il s'agit. Les avis d'Orange auraient alimenté
    les statistiques d'e&, et réciproquement le concurrent aurait paru muet.

    Nommer son propre pays n'a jamais prouvé l'appartenance à un opérateur ;
    nommer un CONCURRENT prouve en revanche la non-appartenance.
    """
    hay = norm(texte)
    siennes = {j for j in norm(sub["operator"]).split() if len(j) >= 3}

    # Si la marque PROPRE de la filiale figure aussi dans le libellé, elle
    # l'emporte : les deux noms cohabitent après un changement d'enseigne.
    # Maroc Telecom a rebaptisé ses filiales « Moov Africa », et l'application
    # mauritanienne s'appelle « My Moov Mauritel » — citer « Moov » n'en fait
    # pas l'application d'un concurrent puisqu'elle se nomme aussi Mauritel.
    if any(_mot(s, hay) for s in siennes):
        return None

    for marque in marques:
        if marque in siennes or len(marque) < 3:
            continue
        if _mot(marque, hay):
            return marque
    return None


def nomme_un_autre_pays(texte: str, iso2: str) -> bool:
    """Le libellé désigne-t-il un pays du périmètre autre que celui attendu ?

    S'appuie sur la table des 54 pays, noms français et anglais — bien plus
    complète que les indices de la découverte, qui laissaient passer « MyMTN
    Congo » pour l'Afrique du Sud.
    """
    hay = norm(texte)
    attendus = {norm(n) for n in COUNTRIES.get(iso2.upper(), ("", "", ""))[:2] if n}
    for autre_iso, (fr, en, _) in COUNTRIES.items():
        if autre_iso == iso2.upper():
            continue
        for nom in (fr, en):
            candidat = norm(nom)
            if len(candidat) < 4 or candidat in attendus:
                continue
            if _mot(candidat, hay):
                return True
    return False


def app_acceptable(app: dict, sub: dict, champ_editeur: str,
                   marques: set[str] = frozenset()) -> tuple[bool, str]:
    """L'application peut-elle être attribuée à cette filiale ?"""
    operateur = norm(sub["operator"])
    nom = app.get("_name") or ""
    editeur = app.get(champ_editeur) or ""
    identifiant = app.get("package_id") or app.get("app_id") or ""
    libelle = f"{nom} {identifiant}"

    # MARQUES DE DEUX CARACTÈRES OU MOINS : aucune découverte automatique.
    #
    # Placé EN TÊTE, et non après le test d'éditeur : « We Ship You » a pour
    # éditeur « We Ship You », où « we » figure comme mot entier — la règle
    # « éditeur = opérateur » l'acceptait donc avant même d'arriver ici, et
    # « WE SOCIETY », « We-Capture » et « We Power » avec elle. « e& » se
    # réduit pour sa part à la lettre « e ».
    #
    # Pour ces marques, la seule preuve recevable est humaine : les
    # applications portant `_verified_on` sont réinjectées en amont et ne
    # passent jamais par cette fonction.
    if len(operateur.replace(" ", "")) <= 2:
        return False, "marque trop courte (verification humaine requise)"

    if nomme_un_autre_pays(libelle, sub["iso2"]):
        return False, "nomme un autre pays"
    hors = nomme_hors_perimetre(libelle)
    if hors:
        return False, f"pays hors perimetre ({hors})"
    concurrent = marque_concurrente(f"{nom} {editeur}", sub, marques)
    if concurrent:
        return False, f"marque concurrente ({concurrent})"

    if _mot(operateur, norm(editeur)):
        return True, "editeur = operateur"

    # La voie « le nom désigne le pays » existe pour les opérateurs qui publient
    # sous leur raison sociale : Orange Sénégal édite sous « SONATEL S.A. », qui
    # ne contient pas « Orange », mais son application s'appelle « Orange et moi
    # Sénégal ». La marque est donc dans le NOM.
    #
    # Elle exigeait seulement que le pays soit nommé, ce qui ne dit rien de
    # l'opérateur — d'où « My Orange Egypt » retenue pour e&. On exige désormais
    # aussi la marque, ce qui reste fidèle au cas d'origine tout en fermant la
    # porte aux applications d'un tiers qui se trouvent nommer le bon pays.
    # Une marque de deux caractères ou moins ne prouve rien dans un nom
    # d'application. « WE » (Telecom Egypt) apparaît comme mot entier dans
    # « We Ship You », « WE SOCIETY » et « We-Capture », trois applications
    # sans rapport que la fusion avait retenues ; « e& » se réduit même à la
    # lettre « e ». Pour ces marques-là, seul l'éditeur fait foi — test déjà
    # tenté ci-dessus, donc arrivé ici c'est un refus.
    if len(operateur.replace(" ", "")) <= 2:
        return False, "marque trop courte, editeur non concordant"

    if app.get("_country_verdict") == "match" and _mot(operateur, norm(nom)):
        return True, "nom = marque + pays"
    if app.get("_country_verdict") == "match":
        return False, "nom designe le pays sans la marque"
    return False, f"editeur tiers ({editeur[:26]})"


def _enrichir_play(retenues: dict) -> None:
    """Ajoute à chaque application Play son nombre d'avis réel.

    Un appel par application. Sans ce chiffre, le plafond couperait au hasard :
    rien dans le résultat de recherche ne distingue l'application self-care de
    l'outil interne destiné aux revendeurs.

    Un échec est sans gravité — l'application garde un compte nul et se
    retrouve en fin de classement, ce qui est le comportement voulu pour une
    application dont on ne sait rien.
    """
    try:
        from google_play_scraper import app as gp_app
    except ImportError:
        print("google_play_scraper absent : classement Play non enrichi")
        return

    total = sum(len(v) for v in retenues.values())
    print(f"Volume d'avis Play Store : {total} application(s) à interroger...")
    fait = 0
    for apps in retenues.values():
        for entree in apps:
            try:
                infos = gp_app(entree["package_id"], lang="fr")
                entree["_play_reviews"] = infos.get("reviews") or 0
                entree["_play_score"] = infos.get("score")
            except Exception:
                entree["_play_reviews"] = 0
            fait += 1
            if fait % 25 == 0:
                print(f"  {fait}/{total}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="ne pas interroger Play pour le volume d'avis")
    args = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    decouvertes = json.loads(DISCOVERED_PATH.read_text(encoding="utf-8"))
    par_nom = {s["subsidiary_name"]: s for s in config["subsidiaries"]}

    # Marques DISTINCTIVES du périmètre, pour repérer l'application d'un
    # concurrent.
    #
    # Prendre tous les jetons des noms d'opérateurs était un piège : « Moov
    # Africa » y injectait « africa », « Djibouti Telecom » y injectait
    # « telecom », « TN Mobile » y injectait « mobile ». L'application
    # officielle « MTN App ZA », éditée par « MTN South Africa », était alors
    # rejetée comme concurrente parce que son éditeur contient « Africa » —
    # 163 rejets à tort, dont les applications principales de MTN, Djezzy et
    # Ooredoo.
    #
    # Deux exclusions, donc : les mots de l'industrie, qui ne distinguent
    # personne, et les noms de pays, qui sont communs à tous les opérateurs
    # d'un même marché.
    banals = {
        "telecom", "telecoms", "telecommunications", "mobile", "mobiles",
        "africa", "african", "afrique", "group", "groupe", "holding", "cell",
        "cellular", "net", "network", "digital", "wireless", "communications",
    }
    noms_pays = {
        norm(n)
        for entree in COUNTRIES.values()
        for n in entree[:2]
        if n
    }
    marques = {
        jeton
        for s in config["subsidiaries"]
        for jeton in norm(s["operator"]).split()
        if len(jeton) >= 3 and jeton not in banals and jeton not in noms_pays
    }

    retenues = {"appstore": defaultdict(list), "playstore": defaultdict(list)}
    rejets = defaultdict(int)

    # LES VÉRIFICATIONS HUMAINES SURVIVENT À LA FUSION.
    #
    # L'écriture finale remplaçait la configuration d'une boutique par le seul
    # résultat de la découverte. Une entrée vérifiée à la main était donc
    # perdue au passage suivant — constaté sur l'Égypte : `558287646` (My e&,
    # éditeur Etisalat Egypt) et `com.ucare.we` (My WE, éditeur Telecom Egypt),
    # confirmés un par un sur la vitrine égyptienne, ont disparu au profit de
    # « TV by e& » et « We Ship You ».
    #
    # C'est l'inversion exacte de la hiérarchie des preuves : une heuristique
    # de rapprochement de noms ne doit jamais écraser une vérification faite
    # contre la boutique réelle. Les applications portant `_verified_on` sont
    # donc réinjectées d'office, sans passer par `app_acceptable` — elles ont
    # déjà subi un contrôle plus strict que le sien.
    conserves = 0
    for nom_filiale, sub in par_nom.items():
        for boutique, cle_id, cle_avis in (
            ("appstore", "app_id", "_store_reviews"),
            ("playstore", "package_id", "_play_reviews"),
        ):
            cfg_boutique = sub["sources"].get(boutique) or {}
            existantes = (cfg_boutique.get("apps")
                          if isinstance(cfg_boutique.get("apps"), list)
                          else [cfg_boutique] if cfg_boutique else [])
            for app in existantes:
                if not isinstance(app, dict) or not app.get("_verified_on"):
                    continue
                garde = dict(app)
                garde.setdefault("_name", app.get("_verified_app"))
                # Le volume vérifié sert de clé de tri, pour que l'application
                # confirmée ne soit pas coupée par le plafond.
                garde.setdefault(cle_avis, app.get("_verified_reviews") or 0)
                retenues[boutique][nom_filiale].append(garde)
                conserves += 1
    if conserves:
        print(f"{conserves} application(s) vérifiée(s) à la main, conservée(s)")

    for nom_filiale, boutiques in decouvertes.items():
        sub = par_nom.get(nom_filiale)
        if not sub:
            continue
        for boutique, champ in (("appstore", "_seller"), ("playstore", "_developer")):
            for app in boutiques.get(boutique, []):
                ok, motif = app_acceptable(app, sub, champ, marques)
                if not ok:
                    rejets[motif.split(" (")[0]] += 1
                    continue
                retenues[boutique][nom_filiale].append(app)

    # RÈGLE 3 — exclusivité des paquets Play Store.
    # Revendications DISTINCTES par filiale.
    #
    # Une liste simple comptait deux fois le même paquet quand une filiale le
    # déclarait à la fois en version vérifiée et en version découverte. La
    # règle y voyait un partage entre deux filiales, ne trouvait pas d'arbitre
    # unique, et retirait le paquet à tout le monde : `com.orange.mobinilandme`
    # disparaissait ainsi d'Orange Égypte, sa seule revendicatrice.
    revendications: dict[str, set[str]] = defaultdict(set)
    for nom_filiale, apps in retenues["playstore"].items():
        for app in apps:
            revendications[app["package_id"]].add(nom_filiale)

    partages = {p: sorted(noms) for p, noms in revendications.items()
                if len(noms) > 1}
    for paquet, noms in partages.items():
        # Une seule filiale le nomme explicitement : elle le garde. Sinon,
        # personne — un paquet ambigu vaut moins que pas de paquet du tout.
        # Une revendication VÉRIFIÉE À LA MAIN tranche avant toute heuristique.
        #
        # Sans cette priorité, `com.orange.mobinilandme` — confirmé sur la
        # boutique égyptienne, éditeur « Orange Egypt », 939 365 avis — était
        # écarté d'Orange Égypte parce qu'une autre filiale Orange le
        # revendiquait aussi et qu'aucune ne portait `_country_verdict`. Le
        # paquet finissait attribué à personne : un contrôle humain réussi
        # produisait moins qu'une découverte automatique.
        verifies = [
            n for n in noms
            if any(a.get("package_id") == paquet and a.get("_verified_on")
                   for a in retenues["playstore"][n])
        ]
        if len(verifies) == 1:
            gagnant = verifies[0]
        else:
            proprietaires = [
                n for n in noms
                if any(a["package_id"] == paquet
                       and a.get("_country_verdict") == "match"
                       for a in retenues["playstore"][n])
            ]
            gagnant = proprietaires[0] if len(proprietaires) == 1 else None
        for n in noms:
            if n == gagnant:
                continue
            retenues["playstore"][n] = [
                a for a in retenues["playstore"][n] if a["package_id"] != paquet
            ]
            rejets["paquet Play partage"] += 1

    # Classement avant plafonnement.
    #
    # Le plafond ne vaut que si les applications sont ordonnées par intérêt
    # réel. Sans classement, on garderait « MTN Online School » et « MoMo Agent
    # App » — outils destinés aux agents et aux élèves — et on couperait
    # « MTN App ZA », l'application self-care que tous les clients utilisent.
    #
    # Le VOLUME D'AVIS est le seul critère non arbitraire : une application que
    # personne ne commente n'apporte rien, quelle que soit son importance
    # supposée. App Store le fournit déjà (`_store_reviews`, vérifié vitrine par
    # vitrine) ; Play Store demande un appel de plus, fait ici.
    if not args.skip_enrich:
        _enrichir_play(retenues["playstore"])

    total = {"appstore": 0, "playstore": 0}
    for boutique in ("appstore", "playstore"):
        cle = "_store_reviews" if boutique == "appstore" else "_play_reviews"
        cle_id = "app_id" if boutique == "appstore" else "package_id"
        for nom_filiale, apps in retenues[boutique].items():
            # Dédoublonnage : une application vérifiée à la main est souvent
            # retrouvée aussi par la découverte. La première occurrence gagne,
            # donc la version vérifiée, qui porte les champs `_verified_*`.
            vues, uniques = set(), []
            for a in apps:
                identifiant = str(a.get(cle_id) or "")
                if identifiant and identifiant not in vues:
                    vues.add(identifiant)
                    uniques.append(a)
            apps = sorted(uniques, key=lambda a: -(a.get(cle) or 0))
            apps = apps[:MAX_APPS_PAR_BOUTIQUE]
            if not apps:
                continue
            par_nom[nom_filiale]["sources"][boutique] = {"apps": apps}
            total[boutique] += len(apps)

    print(f"App Store  : {total['appstore']:4} app(s) retenue(s)")
    print(f"Play Store : {total['playstore']:4} app(s) retenue(s)")
    print(f"Filiales avec au moins une app : "
          f"{sum(1 for s in config['subsidiaries'] if s['sources'].get('appstore') or s['sources'].get('playstore'))}"
          f" / {len(config['subsidiaries'])}")
    print("\nRejets :")
    for motif, n in sorted(rejets.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4}  {motif}")

    if args.dry_run:
        print("\n(dry-run : rien n'a été écrit)")
        return 0

    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nÉcrit dans {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
