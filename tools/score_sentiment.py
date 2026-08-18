"""
Note les moteurs de sentiment contre le jeu de référence.

Sert à trancher une question et une seule : lequel se trompe le moins, et OÙ.
Le détail par langue n'est pas un ornement — c'est la raison d'être du tirage
stratifié. Un moteur peut afficher 85 % au global en étant inutilisable en
arabe, et la moyenne le cacherait.

Le même outil notera le futur modèle local : il suffira de lui passer une
colonne de plus. C'est ce qui rendra la comparaison honnête — même échantillon,
même règle, même code.

Usage :
    python -m tools.score_sentiment
    python -m tools.score_sentiment --gold data/gold/gold_sentiment_20260806.json
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

CLASSES = ("negative", "neutral", "positive")

#: Moteurs présents dans le jeu de référence, et leur libellé. Tout autre champ
#: écrit par `predict_transformer.py` est détecté et noté automatiquement.
MOTEURS = {
    "lexique": "Lexique (VADER maison)",
    "llm_en_base": "LLM déjà en base",
    "_etoiles": "Note en étoiles",
}

#: Champs descriptifs de l'échantillon : jamais des prédictions.
_NON_MOTEURS = {
    "review_id", "text", "langue_probable", "pays", "filiale", "operateur",
    "source", "note_etoiles", "passe_1", "passe_2", "reference", "relecteur",
}


def moteurs_presents(rows: list[dict]) -> dict[str, str]:
    """Moteurs connus, plus tout champ de prédiction ajouté après coup.

    Découvrir les colonnes plutôt que les déclarer évite d'avoir à modifier ce
    fichier — donc de risquer d'y toucher — chaque fois qu'un modèle de plus
    est mis à l'épreuve.
    """
    trouves = dict(MOTEURS)
    for champ in rows[0] if rows else {}:
        if champ not in _NON_MOTEURS and champ not in trouves:
            trouves[champ] = f"Modèle « {champ} »"
    return trouves


def from_rating(note: Optional[int]) -> Optional[str]:
    """Sentiment déduit de la note, selon la règle déjà appliquée en base."""
    if note is None:
        return None
    return "positive" if note >= 4 else "negative" if note <= 2 else "neutral"


def f1_par_classe(paires: list[tuple[str, str]]) -> dict[str, float]:
    """F1 par classe. La justesse globale seule masque l'effondrement d'une
    classe minoritaire — c'est précisément le cas du neutre ici."""
    out = {}
    for c in CLASSES:
        vp = sum(1 for r, p in paires if r == c and p == c)
        fp = sum(1 for r, p in paires if r != c and p == c)
        fn = sum(1 for r, p in paires if r == c and p != c)
        prec = vp / (vp + fp) if vp + fp else 0.0
        rapp = vp / (vp + fn) if vp + fn else 0.0
        out[c] = 2 * prec * rapp / (prec + rapp) if prec + rapp else 0.0
    return out


def evaluer(rows: list[dict], champ: str, verite: str = "reference") -> Optional[dict]:
    paires: list[tuple[str, str]] = []
    par_langue: dict[str, list[tuple[str, str]]] = defaultdict(list)

    for r in rows:
        ref = r.get(verite)
        pred = from_rating(r.get("note_etoiles")) if champ == "_etoiles" else r.get(champ)
        if not ref or not pred:
            continue
        paires.append((ref, pred))
        par_langue[r.get("langue_probable", "?")].append((ref, pred))

    if not paires:
        return None

    justes = sum(1 for r, p in paires if r == p)
    return {
        "compares": len(paires),
        "justesse": 100 * justes / len(paires),
        "f1": f1_par_classe(paires),
        "confusion": Counter(paires),
        "par_langue": {
            lg: (len(v), 100 * sum(1 for r, p in v if r == p) / len(v))
            for lg, v in sorted(par_langue.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Note les moteurs contre la référence.")
    parser.add_argument("--gold", default=None, help="Fichier de référence (défaut : le plus récent).")
    parser.add_argument(
        "--contre",
        default="reference",
        help="Colonne de vérité. « relecteur » note tout le monde contre le "
        "jugement humain, ANNOTATEUR COMPRIS — seule façon de sortir de la "
        "circularité du modèle qui se note lui-même.",
    )
    args = parser.parse_args()

    if args.gold:
        path = Path(args.gold)
    else:
        candidats = sorted(Path("data/gold").glob("gold_sentiment_*.json"))
        if not candidats:
            print("Aucun jeu de référence dans data/gold/.", file=sys.stderr)
            return 1
        path = candidats[-1]

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["reference"]

    print("=" * 72)
    print(f"RÉFÉRENCE : {path.name}")
    print(f"  {payload['stables']} avis retenus sur {payload['annotes']} annotés "
          f"({payload['taux_stabilite']} % de stabilité entre les deux passes)")
    print(f"  annotateur : {payload['modele_annotateur']}")
    print("=" * 72)

    moteurs = moteurs_presents(rows)
    if args.contre != "reference":
        # L'annotateur devient un CONCURRENT dès qu'on le juge de l'extérieur.
        moteurs["reference"] = "Annotateur (Gemini)"
        rows = [r for r in rows if r.get(args.contre)]
        print(f"\nVérité : « {args.contre} » — {len(rows)} avis")

    resultats = {}
    for champ, libelle in moteurs.items():
        if champ == args.contre:
            continue
        res = evaluer(rows, champ, args.contre)
        if res:
            resultats[champ] = (libelle, res)

    print(f"\n{'Moteur':<26}{'Comparés':>10}{'Justesse':>11}"
          f"{'F1 nég':>9}{'F1 neu':>9}{'F1 pos':>9}")
    print("-" * 74)
    for libelle, res in resultats.values():
        f = res["f1"]
        print(f"{libelle:<26}{res['compares']:>10}{res['justesse']:>10.1f}%"
              f"{f['negative']:>9.2f}{f['neutral']:>9.2f}{f['positive']:>9.2f}")

    langues = sorted({lg for _, r in resultats.values() for lg in r["par_langue"]})
    print(f"\n{'Justesse par langue':<26}" + "".join(f"{lg:>12}" for lg in langues))
    print("-" * (26 + 12 * len(langues)))
    for libelle, res in resultats.values():
        ligne = f"{libelle:<26}"
        for lg in langues:
            if lg in res["par_langue"]:
                nb, pct = res["par_langue"][lg]
                ligne += f"{pct:>10.1f}% "
            else:
                ligne += f"{'—':>12}"
        print(ligne)
    print(" " * 26 + "".join(
        f"{'(n=' + str(next((r['par_langue'][lg][0] for _, r in resultats.values() if lg in r['par_langue']), 0)) + ')':>12}"
        for lg in langues
    ))

    for champ, (libelle, res) in resultats.items():
        if champ == "_etoiles":
            continue
        print(f"\n--- {libelle} : où partent les erreurs ---")
        print(f"{'référence \\ prédit':<22}" + "".join(f"{c:>11}" for c in CLASSES))
        for ref in CLASSES:
            ligne = f"{ref:<22}"
            for pred in CLASSES:
                ligne += f"{res['confusion'].get((ref, pred), 0):>11}"
            print(ligne)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
