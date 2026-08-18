"""
Fait prédire un modèle de sentiment sur le jeu de référence, et range le
résultat dans le fichier de référence lui-même.

POURQUOI ÇA N'ÉCRIT PAS EN BASE
    Rien n'est décidé. Tant que le modèle n'a pas battu la barre des 86,5 %
    (le niveau de la note en étoiles), il n'a pas à toucher aux données que le
    tableau de bord affiche. Les prédictions vivent dans le fichier de
    référence, à côté de celles du lexique et du LLM, et `score_sentiment.py`
    les note toutes avec la même règle sur le même échantillon.

LE MODÈLE PAR DÉFAUT
    `cardiffnlp/twitter-xlm-roberta-base-sentiment` : XLM-RoBERTa affiné sur
    ~198 M de messages courts en 8 langues, dont l'arabe, l'anglais, le
    français et le portugais — les quatre groupes du tirage. Le registre
    correspond aussi : messages courts, informels, mal orthographiés, ce que
    sont les avis d'app store.

    `--model` accepte tout autre modèle de classification de séquence ; la
    correspondance des étiquettes est lue dans sa configuration et non
    supposée, faute de quoi une inversion positif/négatif passerait pour une
    contre-performance du modèle.

Usage :
    python -m tools.predict_transformer
    python -m tools.predict_transformer --model <autre> --champ mon_modele
"""

import argparse
import json
import sys
import time
from pathlib import Path

DEFAUT = "cardiffnlp/twitter-xlm-roberta-base-sentiment"

#: Vers nos trois classes. Les modèles publics n'emploient pas tous le même
#: vocabulaire d'étiquettes ; on traduit à partir de ce que le modèle DÉCLARE.
_VERS_CLASSE = {
    "negative": "negative", "neg": "negative", "label_0": "negative",
    "neutral": "neutral", "neu": "neutral", "label_1": "neutral",
    "positive": "positive", "pos": "positive", "label_2": "positive",
}


def resoudre_etiquettes(config) -> dict[int, str]:
    """Traduit les étiquettes déclarées par le modèle vers nos trois classes."""
    brut = getattr(config, "id2label", None) or {}
    sortie: dict[int, str] = {}
    for idx, nom in brut.items():
        clef = str(nom).strip().lower()
        if clef not in _VERS_CLASSE:
            raise SystemExit(
                f"Étiquette « {nom} » inconnue. Complétez _VERS_CLASSE avant "
                "d'utiliser ce modèle — deviner ici fausserait toute la mesure."
            )
        sortie[int(idx)] = _VERS_CLASSE[clef]
    if set(sortie.values()) != {"negative", "neutral", "positive"}:
        raise SystemExit(f"Le modèle ne couvre pas les trois classes : {sortie}")
    return sortie


def main() -> int:
    parser = argparse.ArgumentParser(description="Prédit le sentiment sur le jeu de référence.")
    parser.add_argument("--model", default=DEFAUT)
    parser.add_argument("--champ", default="transformeur", help="Nom de la colonne écrite.")
    parser.add_argument("--gold", default=None)
    parser.add_argument("--batch", type=int, default=16)
    args = parser.parse_args()

    import torch
    from transformers import AutoConfig, AutoModelForSequenceClassification, AutoTokenizer

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
    print(f"Référence : {path.name} — {len(rows)} avis")
    print(f"Modèle    : {args.model}")

    config = AutoConfig.from_pretrained(args.model)
    etiquettes = resoudre_etiquettes(config)
    print(f"Étiquettes: {etiquettes}")

    tok = AutoTokenizer.from_pretrained(args.model)
    model = AutoModelForSequenceClassification.from_pretrained(args.model)
    model.eval()
    torch.set_num_threads(max(1, (torch.get_num_threads() or 4)))

    textes = [(r.get("text") or "").strip() for r in rows]
    debut = time.monotonic()
    predictions: list[str] = []

    with torch.no_grad():
        for i in range(0, len(textes), args.batch):
            lot = textes[i : i + args.batch]
            enc = tok(lot, padding=True, truncation=True, max_length=256, return_tensors="pt")
            logits = model(**enc).logits
            for idx in logits.argmax(dim=-1).tolist():
                predictions.append(etiquettes[idx])
            print(f"  {min(i + args.batch, len(textes))}/{len(textes)}", end="\r")

    duree = time.monotonic() - debut
    print(f"\n{len(textes)} avis en {duree:.1f} s ({len(textes) / duree:.1f} avis/s, CPU)")

    for r, p in zip(rows, predictions):
        r[args.champ] = p

    payload.setdefault("modeles_evalues", {})[args.champ] = {
        "modele": args.model,
        "avis_par_seconde": round(len(textes) / duree, 1),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Écrit dans {path} sous la colonne « {args.champ} ».")
    return 0


if __name__ == "__main__":
    sys.exit(main())
