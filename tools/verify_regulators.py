"""Sonde les sites de régulateurs déclarés dans `config/regulators.json`.

CE QUE CET OUTIL PROUVE, ET CE QU'IL NE PROUVE PAS
    Il prouve qu'une URL répond, qu'elle est servie par le domaine attendu et
    que la page parle bien de régulation des télécommunications. C'est le
    minimum pour éliminer une adresse morte, un domaine expiré ou un parking
    publicitaire.

    Il NE prouve PAS que la liste des opérateurs licenciés figure sur le site,
    ni qu'elle est à jour. Cette vérification-là reste humaine, et c'est elle
    qui autorise à remplir `verifie_le`. Confondre les deux ferait passer 54
    requêtes HTTP réussies pour une validation réglementaire du périmètre —
    exactement le genre de contrôle « crédible et faux » que ce dépôt s'attache
    à empêcher.

USAGE
    python tools/verify_regulators.py               # sonde tout, écrit le fichier
    python tools/verify_regulators.py --pays CI SN  # quelques pays
    python tools/verify_regulators.py --dry-run     # sans réécrire le fichier
"""

import argparse
import datetime as dt
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
import urllib3

RACINE = Path(__file__).resolve().parent.parent
REGISTRE = RACINE / "config" / "regulators.json"

# Beaucoup de sites gouvernementaux africains présentent une chaîne TLS
# incomplète ou un certificat auto-signé. Refuser de les joindre pour cette
# raison n'apprendrait rien sur l'existence du régulateur : on vérifie la
# JOIGNABILITÉ, pas la sécurité du transport. L'avertissement est donc coupé,
# et le fait d'avoir dû ignorer la vérification est consigné dans la sonde.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

#: Vocabulaire attendu sur la page d'accueil d'un régulateur télécom, dans les
#: quatre langues du périmètre. Sa présence distingue un vrai site d'un domaine
#: expiré revendu en page de liens.
VOCABULAIRE = re.compile(
    r"t[ée]l[ée]communicat|telecommunicat|telecomunica|communicat|"
    r"r[ée]gulation|regulat|regula[çc][ãa]o|licence|license|licen[çc]a|"
    r"op[ée]rateur|operator|operador|spectrum|spectre|autorit|authority",
    re.I,
)

ENTETES = {"User-Agent": "Mozilla/5.0 (compatible; analyse-de-sentiment/1.0)"}

#: Libellés de lien qui, sur un site de régulateur, mènent à la liste des
#: titulaires de licences. Quatre langues, car le périmètre en compte quatre.
#: Volontairement étroit : mieux vaut ne rien proposer que d'envoyer le
#: vérificateur humain sur une page d'actualités.
LIENS_LICENCES = re.compile(
    r"(op[ée]rateur|operator|operador|licenc|licens|licenç|"
    r"titulaire|licensee|autoris|habilitad|concession)", re.I,
)


def cherche_page_operateurs(url_base: str, html: str) -> str | None:
    """Meilleur lien de la page d'accueil vers une liste d'opérateurs.

    Retourne None sans hésiter : une proposition fausse coûte plus cher qu'une
    absence de proposition, puisqu'elle sera recopiée telle quelle dans le
    registre et fera croire la localisation faite.
    """
    from urllib.parse import urljoin

    candidats: list[tuple[int, str]] = []
    for href, libelle in re.findall(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S
    ):
        texte = re.sub(r"<[^>]+>", " ", libelle)
        texte = re.sub(r"\s+", " ", texte).strip()
        if not texte or len(texte) > 80:
            continue
        if href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        # Le libellé prime sur l'URL : « Liste des opérateurs » est un signal
        # bien plus sûr qu'un slug qui contient « licence » par hasard.
        score = 2 * bool(LIENS_LICENCES.search(texte)) + bool(
            LIENS_LICENCES.search(href))
        if score:
            candidats.append((score, urljoin(url_base, href)))

    if not candidats:
        return None
    return max(candidats, key=lambda c: c[0])[1]


def sonde(entree: dict) -> dict:
    """Interroge le site d'un régulateur. Ne lève jamais."""
    url = entree.get("site")
    if not url:
        return {"etat": "URL ABSENTE", "verifie_tls": None, "vocabulaire": None,
              "http": None, "url_finale": None, "piste_operateurs": None,
              "le": dt.date.today().isoformat()}

    for verify in (True, False):
        try:
            r = requests.get(url, timeout=30, headers=ENTETES,
                             allow_redirects=True, verify=verify)
        except requests.exceptions.SSLError:
            continue                       # deuxième passe sans vérification TLS
        except requests.RequestException as e:
            return {"etat": f"INJOIGNABLE ({type(e).__name__})",
                    "verifie_tls": None, "vocabulaire": None, "http": None,
                    "url_finale": None, "piste_operateurs": None, "le": dt.date.today().isoformat()}

        texte = r.text if "text" in (r.headers.get("content-type") or "") else ""
        vocab = bool(VOCABULAIRE.search(texte))
        if r.status_code >= 400:
            etat = f"HTTP {r.status_code}"
        elif not texte:
            etat = "REPOND (contenu non textuel)"
        elif vocab:
            etat = "OK"
        else:
            # Répond, mais ne parle pas de télécoms : domaine réattribué, page
            # d'attente, ou contenu chargé en JavaScript. À trancher à l'œil.
            etat = "SUSPECT (vocabulaire absent)"
        return {"etat": etat, "verifie_tls": verify, "vocabulaire": vocab,
                "http": r.status_code, "url_finale": r.url,
                "piste_operateurs": (cherche_page_operateurs(r.url, texte)
                                     if etat == "OK" else None),
                "le": dt.date.today().isoformat()}

    return {"etat": "TLS INVALIDE", "verifie_tls": False, "vocabulaire": None,
            "http": None, "url_finale": None, "piste_operateurs": None, "le": dt.date.today().isoformat()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pays", nargs="*", metavar="ISO2")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    doc = json.loads(REGISTRE.read_text(encoding="utf-8"))
    entrees = doc["regulateurs"]
    filtre = {c.upper() for c in (args.pays or [])}
    cibles = [e for e in entrees if not filtre or e["iso2"] in filtre]

    print(f"{len(cibles)} régulateur(s) à sonder…\n")
    with ThreadPoolExecutor(max_workers=8) as pool:
        for entree, resultat in zip(cibles, pool.map(sonde, cibles)):
            entree["sonde"] = resultat

    largeur = max(len(e["sigle"] or "—") for e in cibles)
    compte: dict[str, int] = {}
    for e in sorted(cibles, key=lambda x: x["iso2"]):
        etat = e["sonde"]["etat"]
        cle = etat.split(" (")[0]
        compte[cle] = compte.get(cle, 0) + 1
        marque = "  " if etat == "OK" else "!!"
        print(f"{marque} {e['iso2']} {(e['sigle'] or '—'):{largeur}} "
              f"{e['pays'][:22]:22} {etat}")

    print("\n" + "=" * 72)
    for etat, k in sorted(compte.items(), key=lambda kv: -kv[1]):
        print(f"  {k:3d}  {etat}")

    pistes = [e for e in cibles if (e["sonde"] or {}).get("piste_operateurs")]
    print(f"\n{len(pistes)} piste(s) de page « opérateurs / licences » "
          f"repérée(s) — à ouvrir et confirmer une par une :")
    for e in sorted(pistes, key=lambda x: x["iso2"]):
        print(f"  {e['iso2']} {e['sonde']['piste_operateurs'][:96]}")

    a_faire = [e for e in entrees if not e.get("verifie_le")]
    print(f"\n{len(a_faire)} pays restent à VALIDER À LA MAIN "
          f"(la sonde ne remplace pas la lecture de la liste de licences).")

    if not args.dry_run:
        REGISTRE.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"Sondes consignées dans {REGISTRE.relative_to(RACINE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
