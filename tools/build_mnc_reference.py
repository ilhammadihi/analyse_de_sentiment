"""Construit la table de référence des réseaux mobiles africains (E.212).

POURQUOI CET OUTIL EXISTE
    `config/operators.json` annonce lui-même sa faiblesse : sa structure est
    « basée sur des connaissances publiques générales, à vérifier ». Personne
    n'a jamais confronté la liste des 132 filiales à une nomenclature externe.
    Tant que ce n'est pas fait, « exhaustif » est une opinion : rien ne dit si
    un opérateur d'un pays a été oublié, ni si un opérateur déclaré existe
    encore.

    L'attribution des codes MCC/MNC par l'UIT (Recommandation E.212) est la
    seule nomenclature qui couvre TOUS les pays du périmètre avec la même
    définition d'« opérateur de réseau mobile ». C'est donc l'étalon retenu
    pour mesurer l'écart — et non pour le combler automatiquement.

CE QUE CETTE SOURCE EST, ET CE QU'ELLE N'EST PAS
    La table est extraite de la page Wikipédia « Mobile network codes in ITU
    region 6xx (Africa) », qui transcrit les assignations E.212 et cite ses
    références ligne par ligne. C'est une TRANSCRIPTION, pas la publication de
    l'UIT.

    La liste officielle (« Mobile Network Codes (MNC) for the international
    identification plan », annexe T-SP-E.212B) n'est pas téléchargeable en
    direct : la page de publication de l'UIT est rendue en JavaScript et
    n'expose aucun lien de fichier au client HTTP. Le Bulletin d'exploitation
    périodique, lui, est accessible en PDF et sert de contrôle ponctuel.

    Conséquence pratique, et elle est structurante : ce que produit cet outil
    est un FAISCEAU D'INDICES pour la revue humaine, jamais une vérité à
    recopier dans `operators.json`. Le champ `provenance` de chaque ligne le
    dit explicitement, pour qu'un lecteur pressé ne puisse pas s'y tromper.

USAGE
    python tools/build_mnc_reference.py            # écrit config/mnc_e212.json
    python tools/build_mnc_reference.py --dry-run  # affiche sans écrire
"""

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "config" / "mnc_e212.json"

SOURCE_URL = (
    "https://en.wikipedia.org/w/index.php"
    "?title=Mobile_network_codes_in_ITU_region_6xx_(Africa)&action=raw"
)
SOURCE_PAGE = (
    "https://en.wikipedia.org/wiki/"
    "Mobile_network_codes_in_ITU_region_6xx_(Africa)"
)

# TOUT titre de niveau 3-4 borne une section. Deux familles seulement :
#   - « ==== [[Algeria]] – DZ ==== » : un pays, code ISO2 exploitable ;
#   - « === A === » (index alphabétique) et « ... – YT/RE ==== » (section
#     multi-territoires) : PAS un pays unique.
#
# Ne détecter que la première famille laisse les lignes de la seconde tomber
# dans la section précédente. Constaté : les six réseaux de Mayotte et de La
# Réunion étaient attribués à l'ÉTHIOPIE, dernier pays reconnu avant elles.
# Aucune erreur n'était levée — même panne silencieuse que les faux amis FIPS
# de `reviews/collectors/countries.py`.
ANY_HEAD = re.compile(r"^={3,4}\s*(.+?)\s*={3,4}\s*$", re.M)
ISO_HEAD = re.compile(r"^(.+?)\s*[–\-]\s*([A-Z]{2})$")


def clean(cell: str) -> str:
    """Retire le balisage wiki d'une cellule, garde le texte lisible."""
    s = cell.strip()
    s = re.sub(r"<ref[^>]*/>", "", s)
    s = re.sub(r"<ref[^>]*>.*?</ref>", "", s, flags=re.S)
    s = re.sub(r"\{\{flagicon\|[^}]*\}\}", "", s, flags=re.I)
    s = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]", r"\1", s)
    s = re.sub(r"\[(?:https?:)?//?\S*\s+([^\]]+)\]", r"\1", s)
    s = re.sub(r"\[(?:https?:)?//\S*\]", "", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    s = re.sub(r"'''?", "", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip(" .")


def parse(wikitext: str) -> tuple[list[dict], int]:
    """Extrait les réseaux. Retourne (réseaux, lignes écartées hors pays)."""
    heads = list(ANY_HEAD.finditer(wikitext))
    reseaux: list[dict] = []
    ignores = 0

    for i, h in enumerate(heads):
        fin = heads[i + 1].start() if i + 1 < len(heads) else len(wikitext)
        bloc = wikitext[h.end():fin]
        m = ISO_HEAD.match(clean(h.group(1)))
        if not m:
            # Compté et non silencieux : une section pays que le motif cesserait
            # de reconnaître doit se voir dans le total, pas disparaître.
            ignores += len(re.findall(r"^\|\s*6\d\d\s*\|\|", bloc, re.M))
            continue
        pays, iso2 = clean(m.group(1)), m.group(2)

        for ligne in re.split(r"^\|-\s*$", bloc, flags=re.M):
            ligne = ligne.strip()
            if not ligne.startswith("|"):
                continue
            cells = [clean(c) for c in ligne.lstrip("|").split("\n")[0].split("||")]
            if len(cells) < 4:
                continue
            mcc, mnc = cells[0], cells[1]
            if not re.fullmatch(r"6\d\d", mcc) or not re.fullmatch(r"\d{1,3}", mnc):
                continue
            reseaux.append({
                "iso2": iso2,
                "pays": pays,
                "mcc": mcc,
                "mnc": mnc.zfill(2),
                "brand": cells[2],
                "operator": cells[3],
                "status": cells[4] if len(cells) > 4 else "",
                "bands": cells[5] if len(cells) > 5 else "",
                "notes": cells[6] if len(cells) > 6 else "",
            })

    return reseaux, ignores


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="affiche le résumé sans écrire le fichier")
    args = ap.parse_args()

    try:
        r = requests.get(SOURCE_URL, timeout=60,
                         headers={"User-Agent": "analyse-de-sentiment/1.0"})
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Source injoignable : {type(e).__name__} — {e}", file=sys.stderr)
        return 2

    reseaux, ignores = parse(r.text)
    if len(reseaux) < 250:
        # Garde-fou : la page a toujours porté ~330 réseaux. Un effondrement
        # signale un changement de mise en forme, pas une vague de résiliations.
        # Écraser la table avec un résultat tronqué serait pire que ne rien
        # faire — l'écart mesuré ensuite paraîtrait crédible et serait faux.
        print(f"ABANDON : {len(reseaux)} réseaux seulement, mise en forme "
              f"probablement modifiée. Table conservée en l'état.",
              file=sys.stderr)
        return 3

    pays = sorted({x["iso2"] for x in reseaux})
    statuts: dict[str, int] = {}
    for x in reseaux:
        statuts[x["status"] or "(vide)"] = statuts.get(x["status"] or "(vide)", 0) + 1

    doc = {
        "_meta": {
            "description": (
                "Réseaux mobiles africains assignés au titre de la "
                "Recommandation UIT-T E.212 (codes MCC/MNC). Sert d'ÉTALON pour "
                "mesurer l'exhaustivité de config/operators.json. Ne jamais "
                "recopier une ligne d'ici vers operators.json sans "
                "confirmation par le régulateur national : voir "
                "config/regulators.json."
            ),
            "provenance": (
                "Transcription communautaire des assignations E.212 "
                "(Wikipédia, région UIT 6xx), et NON la publication de l'UIT. "
                "L'annexe officielle T-SP-E.212B n'est pas téléchargeable en "
                "direct (page rendue en JavaScript)."
            ),
            "source_url": SOURCE_PAGE,
            "extrait_le": dt.date.today().isoformat(),
            "reseaux": len(reseaux),
            "pays": len(pays),
            "lignes_hors_section_pays_ecartees": ignores,
            "statuts": dict(sorted(statuts.items(), key=lambda kv: -kv[1])),
        },
        "reseaux": sorted(reseaux, key=lambda x: (x["iso2"], x["mcc"], x["mnc"])),
    }

    print(f"{len(reseaux)} réseaux sur {len(pays)} pays "
          f"({ignores} ligne(s) hors section pays, écartées)")
    print("statuts :", doc["_meta"]["statuts"])

    if args.dry_run:
        print("\n--dry-run : rien écrit.")
        return 0

    SORTIE.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"\nÉcrit : {SORTIE.relative_to(RACINE)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
