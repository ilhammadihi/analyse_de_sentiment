"""
Construit le JEU DE RÉFÉRENCE qui servira à juger tout moteur de sentiment.

POURQUOI CE FICHIER EXISTE
    On ne peut pas remplacer le lexique par un modèle local sans savoir lequel
    des deux se trompe le plus. Or il n'existe aujourd'hui aucune donnée
    annotée : le seul point de comparaison est un désaccord entre deux moteurs
    (76,4 % d'accord lexique/LLM sur 7 860 avis), et un désaccord ne dit pas
    QUI a tort.

    Ce script produit cette référence : un échantillon stratifié, annoté deux
    fois, dont on ne retient que la partie stable.

CE QUI FAIT LA VALEUR DE CETTE RÉFÉRENCE — et qu'il ne faut pas dégrader

    1. STRATIFICATION. Un tirage uniforme donnerait surtout de l'anglais
       sud-africain : 1 387 avis sur 8 429 en viennent. Un moteur médiocre en
       arabe y obtiendrait un excellent score. On tire donc par groupe de
       langue ET par source.

    2. DOUBLE PASSE EN ORDRE DIFFÉRENT. La seconde passe présente les mêmes
       avis mélangés. Réexécuter à l'identique ne mesurerait que le hasard de
       l'échantillonnage du modèle ; changer l'ordre teste aussi l'effet de
       contexte, qui est la cause réelle d'instabilité d'une annotation par
       lots. Les avis dont les deux passes divergent SONT ÉCARTÉS de la
       référence et conservés à part : ce sont les cas ambigus, et les garder
       reviendrait à mesurer les moteurs sur des questions sans réponse.

    3. SIGNAUX INDÉPENDANTS CONSERVÉS. Chaque ligne porte aussi la note en
       étoiles (produite par un humain, le client lui-même), le verdict du
       lexique et le verdict LLM déjà en base. La note est le seul signal non
       automatique du lot : c'est elle qui permettra de détecter un biais
       systématique de l'annotateur.

    4. RELECTURE HUMAINE. Le script écrit un extrait de 50 avis à relire. Une
       référence annotée par une machine et jamais vérifiée ne prouve rien —
       elle déplace seulement la question.

Usage :
    docker compose exec api python -m tools.build_gold_set --size 500
    docker compose exec api python -m tools.build_gold_set --dry-run
"""

import argparse
import json
import logging
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from reviews.domain.sentiment import analyze_sentiment
from reviews.llm.client import LLMError, LLMUnavailable, get_client
from reviews.log_setup import setup_logging
from reviews.storage.db import get_database

logger = logging.getLogger("build_gold_set")

#: Avis par appel. Même valeur que l'analyse sémantique : au-delà, la sortie
#: dépasse le plafond de jetons et la réponse tronquée n'est plus du JSON.
BATCH_SIZE = 20

#: Caractères arabes. Détecter l'écriture est un FAIT tiré du texte, là où
#: déduire la langue du pays est une supposition — un avis égyptien peut être
#: rédigé en anglais.
_ARABIC_RE = re.compile(r"[؀-ۿ]")

#: Groupes de langue PROBABLE, par pays. C'est un instrument de tirage, pas une
#: affirmation sur la langue d'un avis donné : beaucoup de ces pays sont
#: plurilingues. L'arabe, lui, est détecté sur le texte et prime sur ce tableau.
_LANG_BY_ISO2 = {
    **{c: "ar" for c in ("EG", "DZ", "MA", "TN", "LY", "SD", "MR")},
    **{c: "pt" for c in ("AO", "MZ", "CV", "GW", "ST")},
    **{
        c: "fr"
        for c in (
            "CI", "SN", "ML", "CM", "BF", "NE", "CG", "BJ", "TG", "GN",
            "MG", "TD", "CD", "GA", "DJ", "KM", "CF", "BI",
        )
    },
}

_SYSTEM = """Tu annotes des avis de clients d'opérateurs télécoms africains.

Pour chaque avis, juge la SATISFACTION DU CLIENT ENVERS L'OPÉRATEUR :
- "negative" : le client est mécontent, signale un problème, se plaint
- "positive" : le client est satisfait, remercie, recommande
- "neutral"  : ni l'un ni l'autre — question, constat factuel, avis sans charge

Règles :
- Juge l'intention du client, pas la présence de mots durs. « Le service était
  en panne mais ils ont réparé vite » est positif.
- Une réclamation formulée poliment reste négative.
- Les avis mélangeant plusieurs langues sont fréquents : juge le sens global.
- Si le texte est trop court ou vide de sens, réponds "neutral".

Réponds UNIQUEMENT par un tableau JSON, un objet par avis :
[{"i": 1, "s": "negative"}, {"i": 2, "s": "positive"}]
Aucun texte avant ou après. Aucune explication."""

_VALID = {"negative", "neutral", "positive"}


def lang_group(iso2: Optional[str], text: str) -> str:
    """Groupe de langue servant à stratifier le tirage."""
    if _ARABIC_RE.search(text or ""):
        return "ar"
    return _LANG_BY_ISO2.get((iso2 or "").upper(), "en")


def fetch_pool(db) -> list[dict]:
    """Avis clients exploitables : un texte assez long pour être jugeable."""
    with db.cursor(dict_rows=True) as cur:
        cur.execute(
            """
            SELECT v.review_id,
                   LEFT(v.text, 700) AS text,
                   v.rating,
                   v.iso2,
                   v.country,
                   v.subsidiary,
                   v.operator,
                   v.source_code,
                   v.source,
                   v.sentiment       AS sentiment_affiche,
                   v.lexicon_sentiment,
                   v.llm_sentiment,
                   COALESCE(v.created_at, v.collected_at) AS occurred_at
            FROM v_reviews_enriched v
            WHERE v.source_kind = 'customer_review'
              AND v.text IS NOT NULL
              AND LENGTH(TRIM(v.text)) >= 20
            """
        )
        return [dict(r) for r in cur.fetchall()]


def stratify(pool: list[dict], size: int, seed: int) -> list[dict]:
    """Tire `size` avis en équilibrant les groupes langue × source.

    Répartition PROPORTIONNELLE AUX STRATES, pas au corpus : chaque strate
    reçoit une part égale, plafonnée par ce qu'elle contient réellement. Le
    reliquat des strates trop petites est redistribué aux autres, sans quoi on
    rendrait moins d'avis que demandé dès qu'une langue est peu représentée.
    """
    rng = random.Random(seed)
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in pool:
        strata[(lang_group(r["iso2"], r["text"]), r["source_code"])].append(r)

    for rows in strata.values():
        rng.shuffle(rows)

    chosen: list[dict] = []
    remaining = size
    keys = sorted(strata, key=lambda k: len(strata[k]))  # les plus rares d'abord
    for n, key in enumerate(keys):
        quota = remaining // (len(keys) - n)
        take = min(quota, len(strata[key]))
        chosen.extend(strata[key][:take])
        remaining -= take

    rng.shuffle(chosen)
    return chosen


def _batches(rows: list[dict], n: int):
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def annotate(client, rows: list[dict], pass_name: str) -> dict[str, str]:
    """Une passe d'annotation. Rend {review_id: sentiment}."""
    out: dict[str, str] = {}
    total = (len(rows) + BATCH_SIZE - 1) // BATCH_SIZE

    for num, batch in enumerate(_batches(rows, BATCH_SIZE), start=1):
        listing = "\n".join(
            f"{i}. {(r['text'] or '').strip()}" for i, r in enumerate(batch, start=1)
        )
        try:
            data = client.complete_json(
                system=_SYSTEM,
                user=listing,
                max_tokens=40 * len(batch) + 200,
            )
        except LLMUnavailable as exc:
            logger.error("%s : appel impossible (%s) — arrêt", pass_name, exc)
            break
        except LLMError as exc:
            logger.warning("%s : lot %d/%d en échec (%s) — ignoré", pass_name, num, total, exc)
            continue

        if not isinstance(data, list):
            logger.warning("%s : lot %d — réponse inattendue, ignoré", pass_name, num)
            continue

        for item in data:
            try:
                idx = int(item["i"]) - 1
                verdict = str(item["s"]).strip().lower()
            except (KeyError, TypeError, ValueError):
                continue
            if verdict in _VALID and 0 <= idx < len(batch):
                out[batch[idx]["review_id"]] = verdict

        logger.info("%s : lot %d/%d — %d verdicts", pass_name, num, total, len(out))

    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Construit le jeu de référence annoté du moteur de sentiment."
    )
    parser.add_argument("--size", type=int, default=500, help="Avis à annoter (défaut : 500).")
    parser.add_argument("--seed", type=int, default=20260806, help="Graine du tirage.")
    parser.add_argument("--relecture", type=int, default=50, help="Avis à extraire pour relecture humaine.")
    parser.add_argument("--out", default="data/gold", help="Dossier de sortie.")
    parser.add_argument("--dry-run", action="store_true", help="Tire l'échantillon et chiffre le coût, sans appeler.")
    args = parser.parse_args()

    setup_logging()
    db = get_database()

    pool = fetch_pool(db)
    if not pool:
        logger.error("Aucun avis client exploitable en base.")
        return 1

    sample = stratify(pool, args.size, args.seed)
    repartition = Counter(
        (lang_group(r["iso2"], r["text"]), r["source_code"]) for r in sample
    )

    calls = 2 * ((len(sample) + BATCH_SIZE - 1) // BATCH_SIZE)
    logger.info(
        "Corpus exploitable : %d avis · échantillon : %d · appels nécessaires : %d (2 passes)",
        len(pool), len(sample), calls,
    )
    for (lang, src), nb in sorted(repartition.items(), key=lambda x: -x[1]):
        logger.info("   %-3s %-14s %d", lang, src, nb)

    if args.dry_run:
        logger.info("Passe à vide : rien n'a été appelé.")
        return 0

    client = get_client(db)
    budget = client.remaining_budget()
    if budget < calls:
        logger.error(
            "Budget insuffisant : %d appels restants pour %d nécessaires. "
            "Réduisez --size ou reprenez demain.", budget, calls,
        )
        return 1

    # Passe 2 en ordre différent : c'est l'effet de contexte qu'on cherche à
    # débusquer, pas seulement le hasard d'échantillonnage du modèle.
    shuffled = list(sample)
    random.Random(args.seed + 1).shuffle(shuffled)

    p1 = annotate(client, sample, "passe 1")
    p2 = annotate(client, shuffled, "passe 2")

    stable, instables = [], []
    for r in sample:
        rid = r["review_id"]
        a, b = p1.get(rid), p2.get(rid)
        if a is None or b is None:
            continue
        record = {
            "review_id": rid,
            "text": r["text"],
            "langue_probable": lang_group(r["iso2"], r["text"]),
            "pays": r["country"],
            "filiale": r["subsidiary"],
            "operateur": r["operator"],
            "source": r["source"],
            "note_etoiles": r["rating"],
            "lexique": r["lexicon_sentiment"],
            "llm_en_base": r["llm_sentiment"],
            "passe_1": a,
            "passe_2": b,
        }
        if a == b:
            record["reference"] = a
            stable.append(record)
        else:
            instables.append(record)

    annotes = len(stable) + len(instables)
    if not annotes:
        logger.error("Aucune annotation obtenue — vérifiez la clé et le quota.")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")

    payload: dict[str, Any] = {
        "genere_le": datetime.now(timezone.utc).isoformat(),
        "modele_annotateur": client.cfg.model,
        "graine": args.seed,
        "taille_demandee": args.size,
        "annotes": annotes,
        "stables": len(stable),
        "instables": len(instables),
        "taux_stabilite": round(100 * len(stable) / annotes, 1),
        "reference": stable,
        "ecartes_ambigus": instables,
    }
    gold_path = out_dir / f"gold_sentiment_{stamp}.json"
    gold_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # Extrait de relecture : on privilégie les avis où l'annotateur CONTREDIT
    # la note en étoiles. C'est là qu'une erreur systématique se verrait, et
    # relire cinquante avis pris au hasard n'apprendrait presque rien.
    def contredit_note(r: dict) -> bool:
        note = r["note_etoiles"]
        if note is None:
            return False
        attendu = "positive" if note >= 4 else "negative" if note <= 2 else "neutral"
        return attendu != r["reference"]

    litigieux = [r for r in stable if contredit_note(r)]
    tranquilles = [r for r in stable if not contredit_note(r)]
    random.Random(args.seed).shuffle(tranquilles)
    extrait = (litigieux + tranquilles)[: args.relecture]

    lignes = [
        f"# Relecture du jeu de référence — {stamp}",
        "",
        f"Annotateur : `{client.cfg.model}` · {len(stable)} avis stables sur {annotes} annotés "
        f"({payload['taux_stabilite']} % de stabilité).",
        "",
        "Les avis sont classés en commençant par ceux où l'annotation CONTREDIT la note "
        "en étoiles donnée par le client. C'est là qu'un biais systématique se voit.",
        "",
        "Pour chacun : l'annotation est-elle juste ? Corrigez la colonne si non.",
        "",
    ]
    for i, r in enumerate(extrait, start=1):
        lignes += [
            f"## {i}. {r['filiale'] or r['operateur'] or '—'} · {r['pays'] or '—'} · {r['source']}",
            "",
            f"> {(r['text'] or '').strip()[:500]}",
            "",
            f"- **Référence proposée : `{r['reference']}`**",
            f"- Note du client : {r['note_etoiles'] if r['note_etoiles'] is not None else '—'}/5",
            f"- Lexique : `{r['lexique'] or '—'}` · LLM en base : `{r['llm_en_base'] or '—'}`",
            "- Verdict du relecteur : ",
            "",
        ]
    relecture_path = out_dir / f"relecture_{stamp}.md"
    relecture_path.write_text("\n".join(lignes), encoding="utf-8")

    logger.info("=" * 64)
    logger.info("Annotés          : %d", annotes)
    logger.info("Stables (retenus): %d (%.1f %%)", len(stable), payload["taux_stabilite"])
    logger.info("Écartés ambigus  : %d", len(instables))
    logger.info("Référence        : %s", gold_path)
    logger.info("À relire         : %s (%d avis)", relecture_path, len(extrait))
    logger.info("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
