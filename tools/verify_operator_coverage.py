"""Mesure l'écart entre `config/operators.json` et les assignations E.212.

POURQUOI CET OUTIL EXISTE
    Le périmètre déclare 132 filiales. Rien n'a jamais dit s'il en manquait,
    ni si l'une d'elles avait cessé d'exister. Un pays sous-déclaré ne produit
    aucune erreur : le tableau de bord affiche simplement la satisfaction de
    deux opérateurs là où le marché en compte quatre, et la comparaison entre
    pays — sa raison d'être — devient fausse sans que rien ne le signale.

    L'outil répond à trois questions, et à elles seules :
      1. quels réseaux assignés ne sont PAS déclarés au périmètre ?
      2. quelles filiales déclarées ne correspondent à AUCUN réseau assigné ?
      3. quels codes MCC/MNC rattacher aux filiales déjà déclarées ?

CE QU'IL NE FAIT PAS
    Il ne modifie jamais `operators.json`. Les rapprochements de noms sont des
    PROPOSITIONS : « Moov Africa » retrouve la marque « Moov », « Togocom »
    retrouve « Togocel » par préfixe. Un rapprochement par ressemblance est un
    indice, pas une preuve d'identité juridique — la confirmation vient du
    régulateur national (`config/regulators.json`), jamais d'ici.

PERTINENCE
    223 réseaux sont marqués « Operational », mais beaucoup sont des détenteurs
    de licence régionale ou des réseaux privés sans offre grand public
    (Afrique du Sud : 36 assignations pour 4 opérateurs mobiles réels). Le
    filtre par défaut ne retient donc que les lignes PERTINENTES : une marque
    commerciale renseignée ET des bandes mobiles publiques. `--tous` lève le
    filtre pour l'examen exhaustif.

USAGE
    python tools/verify_operator_coverage.py              # écart, lignes pertinentes
    python tools/verify_operator_coverage.py --tous       # sans filtre de pertinence
    python tools/verify_operator_coverage.py --pays CF ZA # quelques pays
    python tools/verify_operator_coverage.py --json ecart.json
"""

import argparse
import json
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

from reviews.collectors.countries import COUNTRIES  # noqa: E402

OPERATEURS = RACINE / "config" / "operators.json"
REFERENCE = RACINE / "config" / "mnc_e212.json"

# Jetons trop répandus pour identifier un opérateur à eux seuls. Sans cette
# liste, « Orange Mali » et « Malitel » se rapprochent parce que tous deux
# contiennent « telecom ».
BANALS = {
    "sa", "sarl", "ltd", "limited", "plc", "llc", "inc", "spa", "sal", "srl",
    "co", "company", "cie", "group", "groupe", "holding", "holdings", "pty",
    "telecom", "telecoms", "telecommunications", "telecommunication", "telecomm",
    "mobile", "mobiles", "mobil", "cellular", "communications", "communication",
    "network", "networks", "wireless", "africa", "african", "afrique",
    "national", "nationale", "societe", "de", "du", "des", "la", "le", "les",
    "and", "the", "of", "for", "new", "sud", "south", "republic", "rep",
    "operator", "services", "service", "digital", "international",
}

#: Une bande mobile grand public. Distingue un opérateur d'un détenteur de
#: licence : « Unknown » ou « WiMAX » ne dessert pas un marché de particuliers.
BANDES_PUBLIQUES = re.compile(r"\b(GSM|UMTS|LTE|NR|CDMA|HSPA)\b", re.I)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def tokens(s: str, exclus: frozenset[str] = frozenset()) -> set[str]:
    # Seuil à 2 et non 3 : « TC » (Telecel Centrafrique), « AT » (AirtelTigo
    # Ghana) et « TN Mobile » (Namibie) sont des marques réelles qu'un seuil
    # plus haut réduit à l'ensemble vide, donc à « aucune correspondance ».
    return {t for t in norm(s).split()
            if len(t) >= 2 and t not in BANALS and t not in exclus}


def noms_pays(iso2: str, *libelles: str) -> frozenset[str]:
    """Jetons du nom du pays, à NEUTRALISER dans le rapprochement.

    Les `subsidiary_name` du périmètre sont bâtis sur le modèle « Opérateur +
    Pays » (« Moov Africa Centrafrique »), et la colonne exploitant de la table
    E.212 l'est souvent aussi (« Orange Egypt »). Sans neutralisation, le nom du
    pays devient un jeton commun à TOUS les opérateurs du pays : « Orange
    Egypt » s'appariait ainsi à « e& Egypt », masquant l'absence d'un opérateur
    égyptien majeur derrière un faux appariement.
    """
    jetons = set()
    for libelle in libelles:
        jetons |= {t for t in norm(libelle).split() if len(t) >= 2}
    entree = COUNTRIES.get(iso2.upper())
    if entree:
        for libelle in entree[:2]:          # noms français et anglais
            jetons |= {t for t in norm(libelle).split() if len(t) >= 2}
    return frozenset(jetons)


def apparie(declare: str, reseau: str,
            exclus: frozenset[str] = frozenset()) -> bool:
    """Vrai si les deux désignations PEUVENT viser le même opérateur."""
    jd, jr = tokens(declare, exclus), tokens(reseau, exclus)

    # Neutraliser le nom du pays est indispensable (« Orange Egypt » ne doit
    # pas s'apparier à « e& Egypt »), mais certains opérateurs n'ont PAS d'autre
    # nom que celui de leur pays : « Maroc Telecom », « Djibouti Telecom »,
    # « Comores Telecom ». Pour eux la neutralisation vide l'ensemble et rend
    # tout appariement impossible — neuf incumbents ressortaient ainsi comme
    # « sans réseau assigné ».
    #
    # D'où la règle : on ne revient au nom du pays que si les DEUX côtés sont
    # vides, cas où il est le seul identifiant disponible et où s'en servir est
    # légitime. Si un seul côté est vide, l'autre porte une marque distincte et
    # l'appariement serait abusif.
    if not jd and not jr:
        jd, jr = tokens(declare), tokens(reseau)
    if jd & jr:
        return True
    # Le nom du pays doit AUSSI disparaître des chaînes compactées, sinon le
    # test d'inclusion le réintroduit par la bande : « egypt » est contenu dans
    # « orangeegypt », et le faux appariement que les jetons viennent d'écarter
    # revient intact.
    a, b = "".join(sorted(jd)), "".join(sorted(jr))
    if not a or not b:
        return False
    if len(a) >= 5 and len(b) >= 5:
        commun = 0
        while commun < min(len(a), len(b)) and a[commun] == b[commun]:
            commun += 1
        if commun >= 5:                      # « togocom » / « togocel »
            return True
    # Seuil à 3 : « CST » (São Tomé) est inclus dans « CSTmovel », et un seuil
    # de 4 rejetait ce rapprochement pourtant évident — la filiale ressortait
    # alors comme « sans réseau assigné » alors que le sien était juste à côté.
    return min(len(a), len(b)) >= 3 and (a in b or b in a)


def pertinent(n: dict) -> bool:
    """Réseau susceptible d'avoir une clientèle grand public observable.

    Le seul critère est la présence de BANDES mobiles publiques. Exiger en plus
    une marque commerciale paraissait raisonnable — les détenteurs de licence
    sans offre grand public n'en ont pas — mais écartait Africell Angola, dont
    la ligne source ne renseigne que l'exploitant. Or les bandes suffisent
    déjà : les licences régionales sud-africaines portent « Unknown », et les
    réseaux fixes « WiMAX ». Le critère de marque n'excluait donc plus que de
    vrais opérateurs.
    """
    if n["status"].strip().lower() in {"not operational", "reserved"}:
        return False
    return bool(BANDES_PUBLIQUES.search(n["bands"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tous", action="store_true",
                    help="ne pas filtrer sur la pertinence grand public")
    ap.add_argument("--pays", nargs="*", metavar="ISO2",
                    help="restreindre à quelques pays")
    ap.add_argument("--json", metavar="FICHIER",
                    help="écrire l'écart complet en JSON")
    args = ap.parse_args()

    if not REFERENCE.exists():
        print("config/mnc_e212.json absent — lancer d'abord "
              "tools/build_mnc_reference.py", file=sys.stderr)
        return 2

    ref = json.loads(REFERENCE.read_text(encoding="utf-8"))
    cfg = json.loads(OPERATEURS.read_text(encoding="utf-8"))

    filtre = {c.upper() for c in (args.pays or [])}
    assignes: dict[str, list[dict]] = defaultdict(list)
    for n in ref["reseaux"]:
        if filtre and n["iso2"] not in filtre:
            continue
        if args.tous or pertinent(n):
            assignes[n["iso2"]].append(n)

    declares: dict[str, list[dict]] = defaultdict(list)
    for s in cfg["subsidiaries"]:
        if filtre and s["iso2"] not in filtre:
            continue
        declares[s["iso2"]].append(s)

    ecart = {"manquants": [], "sans_mnc": [], "apparies": [], "pays": {}}

    for iso2 in sorted(set(assignes) | set(declares)):
        nets, subs = assignes.get(iso2, []), declares.get(iso2, [])
        pays = nets[0]["pays"] if nets else subs[0]["country"]
        exclus = noms_pays(iso2, pays, *(s["country"] for s in subs))
        pris: set[int] = set()

        for sub in subs:
            # TOUS les réseaux de l'opérateur, pas seulement le premier : une
            # société détient couramment plusieurs MNC (Orange RDC en a deux).
            # S'arrêter au premier faisait passer les suivants pour des
            # opérateurs « non déclarés ».
            trouves = [
                i for i, n in enumerate(nets)
                if apparie(sub["operator"], n["brand"], exclus)
                or apparie(sub["operator"], n["operator"], exclus)
                or apparie(sub["subsidiary_name"], n["brand"], exclus)
                or apparie(sub["subsidiary_name"], n["operator"], exclus)
            ]
            if not trouves:
                ecart["sans_mnc"].append({
                    "iso2": iso2, "pays": pays,
                    "operateur": sub["operator"],
                    "filiale": sub["subsidiary_name"],
                })
                continue
            pris.update(trouves)
            ecart["apparies"].append({
                "iso2": iso2, "operateur": sub["operator"],
                "filiale": sub["subsidiary_name"],
                "reseaux": [
                    {k: nets[i][k] for k in
                     ("mcc", "mnc", "brand", "operator", "status")}
                    for i in trouves
                ],
            })

        for i, n in enumerate(nets):
            if i not in pris:
                ecart["manquants"].append({
                    "iso2": iso2, "pays": pays, "mcc": n["mcc"], "mnc": n["mnc"],
                    "brand": n["brand"], "exploitant": n["operator"],
                    "status": n["status"], "bandes": n["bands"],
                    "notes": n["notes"],
                })

        ecart["pays"][iso2] = {"pays": pays, "assignes": len(nets),
                               "declares": len(subs)}

    print(f"{'TOUS les réseaux' if args.tous else 'Réseaux grand public'} — "
          f"{len(ecart['pays'])} pays")
    print(f"  filiales appariées à au moins un MNC : {len(ecart['apparies'])}")
    print(f"  filiales sans aucun MNC              : {len(ecart['sans_mnc'])}")
    print(f"  réseaux assignés non déclarés        : {len(ecart['manquants'])}")

    if ecart["manquants"]:
        print("\n--- RESEAUX ASSIGNES ABSENTS DU PERIMETRE ---")
        par_pays: dict[str, list[dict]] = defaultdict(list)
        for m in ecart["manquants"]:
            par_pays[m["iso2"]].append(m)
        for iso2, items in sorted(par_pays.items(),
                                  key=lambda kv: (-len(kv[1]), kv[0])):
            info = ecart["pays"][iso2]
            print(f"  {iso2} {info['pays'][:26]:26s} "
                  f"declares {info['declares']:2d} / assignes {info['assignes']:2d}")
            for m in items:
                nom = m["brand"] or m["exploitant"]
                print(f"       + {m['mcc']}-{m['mnc']}  {nom[:30]:30s} "
                      f"{m['status'][:15]:15s} {m['exploitant'][:34]}")

    if ecart["sans_mnc"]:
        print("\n--- FILIALES DECLAREES SANS RESEAU ASSIGNE (a verifier) ---")
        for s in ecart["sans_mnc"]:
            print(f"  {s['iso2']} {s['operateur']:24s} ({s['filiale']})")

    if args.json:
        Path(args.json).write_text(
            json.dumps(ecart, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nÉcart complet écrit dans {args.json}")

    # Code de sortie non nul si le périmètre est incomplet : utilisable en
    # contrôle périodique, au même titre que verify_gdelt_countries.py.
    return 1 if ecart["manquants"] or ecart["sans_mnc"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
