"""
Enrichit le lexique de sentiment à partir du corpus, par apprentissage supervisé.

POURQUOI CET OUTIL EXISTE
    Le lexique écrit à la main atteignait 42,7 % d'exactitude sur un jeu de test
    — moins bien qu'un classifieur trivial qui répondrait toujours « négatif »
    (52,4 %). La cause, mesurée : 95,9 % des avis 1-2 étoiles mal classés ne
    déclenchaient AUCUN mot du lexique. Ce dernier avait été écrit avec du
    vocabulaire télécom (panne, coupure, réseau) alors que les avis viennent de
    boutiques d'applications et parlent de l'APP : « keeps crashing »,
    « doesn't open », « unable to process ». S'y ajoutaient une couverture
    anglaise deux fois plus mince que la française et l'arabe absent, alors que
    cinq pays du périmètre écrivent en arabe.

LA NOTE SERT D'ÉTIQUETTE, JAMAIS DE SIGNAL
    Les notes des clients servent ici d'étiquettes d'apprentissage pour
    découvrir quels mots séparent réellement un mécontent d'un satisfait. Le
    classifieur produit reste PUREMENT TEXTUEL : à l'exécution, aucune note
    n'est lue. C'est une exigence du projet, et elle est tenue.

MÉTHODE
    Rapport de cotes logarithmique entre avis 1-2 étoiles et avis 4-5 étoiles,
    avec lissage de Laplace. Un mot fréquent des deux côtés (« application »,
    « telephone ») obtient un score proche de zéro et n'est pas retenu ; un mot
    déséquilibré (« crashing », « impossible ») ressort fortement.

MESURE HONNÊTE
    Découpage 80/20. Les termes sont découverts sur la partie apprentissage
    UNIQUEMENT et l'exactitude est mesurée sur la partie test, jamais vue.
    Mesurer sur les données d'apprentissage produirait un chiffre flatteur et
    faux.

USAGE
    python -m tools.build_lexicon              # mesure et écrit le lexique
    python -m tools.build_lexicon --dry-run    # mesure seulement
"""

import argparse
import json
import math
import random
import re
import sys
import unicodedata
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reviews.storage.db import get_database  # noqa: E402

SORTIE = ROOT / "config" / "lexicon_learned.json"

#: Tokeniseur élargi à l'ARABE (U+0600–U+06FF). Celui du moteur ne reconnaît
#: que l'alphabet latin : sur un avis égyptien ou soudanais il ne produit aucun
#: jeton, donc aucun sentiment.
TOKEN_RE = re.compile(r"[a-zà-öø-ÿ']+|[؀-ۿ]+", re.IGNORECASE)

#: Occurrences minimales pour qu'un mot soit un signal et non du bruit.
MIN_OCCURRENCES = 10

#: Nombre de termes retenus par polarité.
MAX_TERMES = 350

#: Mots-outils exclus du lexique.
#:
#: Ils sont pourtant STATISTIQUEMENT discriminants — « told », « keeps »,
#: « cannot » apparaissent massivement dans les avis mécontents (« keeps
#: crashing », « told me to wait »). On les écarte quand même, parce que le
#: lexique a un second usage : alimenter l'onglet « Motifs d'insatisfaction ».
#: Or « told » n'est pas un motif de mécontentement, c'est un verbe. Les
#: afficher rendrait cet écran incompréhensible pour un lecteur métier.
#:
#: C'est un arbitrage assumé entre exactitude et lisibilité, et son coût est
#: mesuré à chaque exécution de cet outil.
MOTS_OUTILS = {
    # anglais
    "the", "and", "for", "you", "your", "this", "that", "with", "from", "have",
    "has", "had", "was", "were", "are", "but", "not", "all", "any", "can",
    "cannot", "cant", "could", "would", "should", "will", "just", "now", "then",
    "there", "their", "them", "they", "what", "when", "where", "which", "who",
    "why", "how", "get", "got", "getting", "give", "given", "make", "makes",
    "made", "take", "takes", "keep", "keeps", "kept", "told", "tell", "tells",
    "say", "says", "said", "want", "wants", "need", "needs", "try", "tried",
    "try", "use", "used", "using", "see", "seen", "know", "even", "still",
    "every", "always", "never", "after", "before", "again", "back", "one",
    "two", "time", "times", "day", "days", "week", "month", "year", "please",
    "app", "application", "phone", "mobile", "network", "data", "money",
    "account", "service", "customer", "number", "update", "version", "line",
    # Passés au travers du premier filtre, et remontés en TÊTE des motifs :
    # « work », « been », « like », « open »… Statistiquement forts, vides de
    # sens pour un lecteur métier.
    "work", "works", "working", "worked", "been", "being", "like", "likes",
    "open", "opens", "opened", "opening", "close", "closed", "put", "come",
    "comes", "went", "goes", "going", "let", "lets", "much", "many", "more",
    "most", "less", "least", "only", "also", "well", "very", "too", "than",
    "then", "than", "each", "other", "another", "same", "such", "own", "way",
    "thing", "things", "something", "anything", "nothing", "everything",
    "people", "person", "someone", "anyone", "everyone", "here", "there",
    "out", "off", "over", "under", "into", "onto", "about", "around", "down",
    "away", "through", "while", "since", "until", "because", "though",
    # français
    "les", "des", "une", "pour", "avec", "dans", "sur", "par", "que", "qui",
    "est", "sont", "etre", "avoir", "fait", "faire", "plus", "moins", "tres",
    "trop", "bien", "mal", "tout", "tous", "toute", "meme", "aussi", "encore",
    "deja", "apres", "avant", "quand", "alors", "donc", "mais", "car", "sans",
    "chez", "vous", "nous", "ils", "elle", "elles", "mon", "mes", "son", "ses",
    "leur", "cette", "quoi", "jour", "jours", "fois", "temps", "application",
    "reseau", "forfait", "credit", "compte", "client", "service", "numero",
    "telephone", "internet", "connexion", "abonnement", "operateur", "agence",
    "pas", "non", "rien", "aucun", "aucune", "jamais", "toujours", "etait",
    "avait", "peut", "peux", "puis", "veut", "veux", "dire", "voir", "aller",
    "mettre", "prendre", "donner", "passer", "rester", "chose", "choses",
    "gens", "personne", "monde", "part", "cote", "point", "cas", "facon",
    # arabe (mots grammaticaux et topiques fréquents)
    "في", "من", "على", "الى",
    "بعد", "مع", "عن", "هذا",
    "هذه", "التي", "الذي",
    "شركة", "خدمة", "البرنامج",
    "عملاء", "التطبيق",
}


def marques_exclues() -> set[str]:
    """Noms d'opérateurs et de filiales, chargés depuis la configuration.

    Une marque n'est JAMAIS un motif d'insatisfaction : « orange » remontait en
    quatrième position des motifs, simplement parce que les avis mécontents
    nomment plus souvent l'opérateur que les avis satisfaits. C'est un fait
    statistique vrai et une information inutile.

    Lu depuis config/operators.json plutôt qu'écrit en dur : ajouter un
    opérateur au périmètre l'exclut automatiquement des motifs, sans que
    personne ait à y penser.
    """
    chemin = ROOT / "config" / "operators.json"
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    mots: set[str] = set()
    for sub in data.get("subsidiaries", []):
        for champ in (sub.get("subsidiary_name", ""), sub.get("operator", ""),
                      sub.get("country", "")):
            for mot in norm(champ).split():
                if len(mot) >= 3:
                    mots.add(mot)
    return mots


def norm(texte: str) -> str:
    """Minuscules, accents latins retirés, caractères arabes préservés."""
    texte = unicodedata.normalize("NFKD", texte or "")
    return "".join(c for c in texte if not unicodedata.combining(c)).lower()


def jetons(texte: str) -> set[str]:
    """Jetons DISTINCTS d'un avis.

    Un ensemble et non une liste : répéter « nul » cinq fois dans un avis n'en
    fait pas un signal cinq fois plus fort, cela reflète l'emportement de son
    auteur. Compter les avis qui emploient un mot, pas les emplois du mot.
    """
    return set(TOKEN_RE.findall(norm(texte)))


def etiquette(note: int) -> str:
    return "negative" if note <= 2 else ("positive" if note >= 4 else "neutral")


def charger_corpus() -> list[tuple[str, int]]:
    with get_database().cursor() as cur:
        cur.execute(
            """
            SELECT r.text, r.rating
            FROM reviews r JOIN dim_source s ON s.source_id = r.source_id
            WHERE s.kind = 'customer_review' AND r.rating IS NOT NULL
              AND length(r.text) > 10
            """
        )
        return cur.fetchall()


MARQUES: set[str] = set()   # rempli au démarrage par main()


def apprendre(apprentissage: list[tuple[str, int]]) -> dict[str, float]:
    """Log-odds de chaque terme entre avis mécontents et satisfaits."""
    neg_c, pos_c = Counter(), Counter()
    n_neg = n_pos = 0
    for texte, note in apprentissage:
        lab = etiquette(note)
        if lab == "neutral":
            continue  # les 3 étoiles n'aident pas à séparer les extrêmes
        for j in jetons(texte):
            (neg_c if lab == "negative" else pos_c)[j] += 1
        if lab == "negative":
            n_neg += 1
        else:
            n_pos += 1

    poids: dict[str, float] = {}
    for mot in set(neg_c) | set(pos_c):
        if mot in MOTS_OUTILS or mot in MARQUES or len(mot) < 3:
            continue
        if neg_c[mot] + pos_c[mot] < MIN_OCCURRENCES:
            continue
        # Lissage de Laplace : un mot vu 12 fois d'un seul côté ne doit pas
        # produire un poids infini.
        p_neg = (neg_c[mot] + 1) / (n_neg + 2)
        p_pos = (pos_c[mot] + 1) / (n_pos + 2)
        # SIGNE INVERSÉ à dessein. Le log-odds brut est POSITIF pour un mot de
        # mécontentement (plus fréquent chez les insatisfaits), alors que le
        # moteur code le mécontentement par un poids NÉGATIF, comme le lexique
        # écrit à la main (« arnaque » : -2,5). Émettre la convention du moteur
        # ici évite d'avoir à s'en souvenir au chargement — une inversion
        # oubliée retournerait tous les sentiments sans lever la moindre erreur.
        poids[mot] = round(-math.log(p_neg / p_pos), 3)

    # On ne garde que les extrêmes : un terme au log-odds proche de zéro est
    # employé également des deux côtés et n'apporte rien.
    # Après inversion : les plus NÉGATIFS sont les motifs de mécontentement,
    # les plus positifs les marques de satisfaction.
    tries = sorted(poids.items(), key=lambda kv: kv[1])
    retenus = dict(tries[:MAX_TERMES] + tries[-MAX_TERMES:])
    return retenus


def regler_seuils(apprentissage, poids) -> tuple[float, float]:
    """Choisit les bornes de la zone neutre SUR L'APPRENTISSAGE.

    Les régler sur le test reviendrait à s'entraîner dessus : le chiffre
    d'exactitude annoncé ne voudrait plus rien dire.
    """
    valeurs = [(sum(poids.get(j, 0.0) for j in jetons(t)), etiquette(n))
               for t, n in apprentissage]
    meilleur, arg = -1, (-0.5, 0.5)
    for seuil_neg in [-x * 0.25 for x in range(1, 17)]:
        for seuil_pos in [x * 0.25 for x in range(1, 17)]:
            bon = sum(
                1 for s, v in valeurs
                if (s <= seuil_neg and v == "negative")
                or (s >= seuil_pos and v == "positive")
                or (seuil_neg < s < seuil_pos and v == "neutral")
            )
            if bon > meilleur:
                meilleur, arg = bon, (seuil_neg, seuil_pos)
    return arg


def evaluer(test, poids, haut, bas) -> tuple[float, float]:
    bon = neutres = 0
    for texte, note in test:
        s = sum(poids.get(j, 0.0) for j in jetons(texte))
        p = "negative" if s <= haut else ("positive" if s >= bas else "neutral")
        bon += (p == etiquette(note))
        neutres += (p == "neutral")
    return 100 * bon / len(test), 100 * neutres / len(test)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="mesure sans écrire")
    args = ap.parse_args()

    global MARQUES
    MARQUES = marques_exclues()
    print(f"Marques exclues des motifs : {len(MARQUES)} mots (opérateurs, filiales, pays)")

    corpus = charger_corpus()
    random.seed(42)  # découpage reproductible : deux exécutions comparables
    random.shuffle(corpus)
    coupe = int(len(corpus) * 0.8)
    app, test = corpus[:coupe], corpus[coupe:]
    print(f"Corpus : {len(corpus)} avis notés — {len(app)} apprentissage / {len(test)} test")

    poids = apprendre(app)
    seuil_neg, seuil_pos = regler_seuils(app, poids)
    exactitude, part_neutre = evaluer(test, poids, seuil_neg, seuil_pos)

    # Référence honnête : que ferait un classifieur trivial ?
    trivial = 100 * sum(1 for _, n in test if etiquette(n) == "negative") / len(test)

    arabes = sum(1 for m in poids if any("؀" <= c <= "ۿ" for c in m))
    print(f"Termes retenus : {len(poids)} (dont {arabes} en caractères arabes)")
    print(f"Seuils réglés sur l'apprentissage : négatif <= {seuil_neg:+.2f} · positif >= {seuil_pos:+.2f}")
    print()
    print(f"  toujours « négatif » (référence) : {trivial:5.1f} %")
    print(f"  lexique appris                   : {exactitude:5.1f} %  "
          f"({part_neutre:.0f} % classés neutres)")

    if args.dry_run:
        print("\nDRY-RUN : rien écrit.")
        return 0

    SORTIE.write_text(json.dumps({
        "_meta": {
            "genere_le": date.today().isoformat(),
            "methode": "log-odds sur notes clients, 80/20, mots-outils exclus",
            "corpus": len(corpus),
            "exactitude_test": round(exactitude, 1),
            "reference_triviale": round(trivial, 1),
            "seuil_negatif": seuil_neg,
            "seuil_positif": seuil_pos,
        },
        "poids": poids,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nÉcrit : {SORTIE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
