"""
Réintègre les verdicts du relecteur humain dans le jeu de référence.

POURQUOI CETTE ÉTAPE EXISTE
    Le jeu de référence a été annoté par un modèle. Comparer ce modèle à
    lui-même donne 96,4 % et ne prouve rien. Le verdict humain est le seul
    jugement extérieur à la boucle : c'est lui qui dit si l'avance du LLM est
    réelle ou si elle n'est qu'un reflet.

    Une fois ingérés, les verdicts deviennent une colonne de vérité de plus, et
    `score_sentiment.py --contre relecteur` note TOUS les moteurs contre eux,
    l'annotateur compris.

APPARIEMENT PAR LE TEXTE
    Le fichier de relecture ne porte pas les identifiants — ils n'auraient rien
    dit au relecteur et auraient encombré la lecture. L'appariement se fait donc
    sur le texte, tronqué à 500 caractères comme à l'écriture. Toute entrée non
    appariée est SIGNALÉE et non silencieusement ignorée : perdre un verdict
    humain sans le dire fausserait la mesure qui suit.

Usage :
    python -m tools.ingest_relecture
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

VALIDES = {"negative", "neutral", "positive"}

_BLOC = re.compile(r"^## \d+\. ", re.MULTILINE)
_CITATION = re.compile(r"^> (.*)$", re.MULTILINE)
_VERDICT = re.compile(r"Verdict du relecteur\s*:\s*`?([a-zA-Z]*)`?\s*$", re.MULTILINE)
_REFERENCE = re.compile(r"Référence proposée\s*:\s*`([a-z]+)`")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingère les verdicts humains.")
    parser.add_argument("--relecture", default=None)
    parser.add_argument("--gold", default=None)
    args = parser.parse_args()

    dossier = Path("data/gold")
    rel_path = Path(args.relecture) if args.relecture else max(dossier.glob("relecture_*.md"), default=None)
    gold_path = Path(args.gold) if args.gold else max(dossier.glob("gold_sentiment_*.json"), default=None)
    if not rel_path or not gold_path:
        print("Fichier de relecture ou de référence introuvable.", file=sys.stderr)
        return 1

    contenu = rel_path.read_text(encoding="utf-8")
    blocs = _BLOC.split(contenu)[1:]

    verdicts: list[tuple[str, str, str]] = []  # (texte, verdict, reference)
    vides = 0
    invalides: list[str] = []

    for bloc in blocs:
        cite = _CITATION.search(bloc)
        verdict = _VERDICT.search(bloc)
        ref = _REFERENCE.search(bloc)
        if not cite:
            continue
        brut = (verdict.group(1) if verdict else "").strip().lower()
        if not brut:
            vides += 1
            continue
        if brut not in VALIDES:
            invalides.append(brut)
            continue
        verdicts.append((cite.group(1).strip(), brut, ref.group(1) if ref else ""))

    payload = json.loads(gold_path.read_text(encoding="utf-8"))
    rows = payload["reference"]

    # Index par préfixe de texte, tel qu'écrit dans le fichier de relecture.
    index = {(r.get("text") or "").strip()[:500]: r for r in rows}

    appliques, orphelins = 0, []
    for texte, verdict, _ in verdicts:
        cible = index.get(texte)
        if cible is None:
            orphelins.append(texte[:70])
            continue
        cible["relecteur"] = verdict
        appliques += 1

    gold_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    notes = [r for r in rows if r.get("relecteur")]
    accord = sum(1 for r in notes if r["relecteur"] == r["reference"])

    print(f"Fichier relu     : {rel_path.name}")
    print(f"Blocs trouvés    : {len(blocs)}")
    print(f"Verdicts remplis : {len(verdicts)}   (non remplis : {vides})")
    if invalides:
        print(f"  ! valeurs non reconnues : {Counter(invalides)}")
    print(f"Appliqués        : {appliques}")
    if orphelins:
        print(f"  ! {len(orphelins)} non appariés :")
        for o in orphelins:
            print(f"      {o}…")
    print()
    if notes:
        print(f"Accord relecteur / annotateur : {accord}/{len(notes)} "
              f"({100 * accord / len(notes):.1f} %)")
        desaccords = [r for r in notes if r["relecteur"] != r["reference"]]
        if desaccords:
            print(f"\n--- {len(desaccords)} désaccord(s) avec l'annotateur ---")
            for r in desaccords:
                print(f"  annotateur={r['reference']:<9} relecteur={r['relecteur']:<9} "
                      f"note={r.get('note_etoiles')} · {(r.get('text') or '')[:70]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
