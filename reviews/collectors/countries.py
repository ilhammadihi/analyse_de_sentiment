"""
Table pays du périmètre : nom français, nom anglais, code FIPS.

UNE table, trois usages :

* le nom FRANÇAIS est celui de `config/operators.json` ;
* le nom ANGLAIS sert à rattacher un article de presse anglophone à la bonne
  filiale (« MTN Nigeria » dans TechCabal, « Vodacom South Africa » dans
  TechCentral) — sans lui, les six flux anglophones sur dix ne rattachent rien ;
* le code FIPS 10-4 est ce qu'attend l'opérateur `sourcecountry:` de GDELT.

⚠️ FIPS N'EST PAS ISO 3166. Sept codes du périmètre sont des faux amis, où le
code FIPS d'un pays est l'ISO2 d'un AUTRE pays de la même liste :

    ISO2  pays                        FIPS   ...mais 'FIPS' est l'ISO2 de
    ----  --------------------------  ----   ---------------------------
    ZA    Afrique du Sud              SF     (et ZA est le FIPS de la Zambie)
    ZM    Zambie                      ZA     Afrique du Sud
    NG    Nigeria                     NI     (et NG est le FIPS du Niger)
    NE    Niger                       NG     Nigeria
    CD    RD Congo                    CG     Congo-Brazzaville
    CG    Congo-Brazzaville           CF     Centrafrique
    CF    Centrafrique                CT     —
    GA    Gabon                       GB     —  (et GA est le FIPS de la Gambie)
    GM    Gambie                      GA     Gabon
    MA    Maroc                       MO     —  (et MA est le FIPS de Madagascar)
    MG    Madagascar                  MA     Maroc

Confondre les deux ne lève AUCUNE erreur : GDELT renvoie simplement les
articles d'un autre pays, qui sont alors attribués à la mauvaise filiale. C'est
la panne la plus coûteuse imaginable pour un tableau de bord dont la raison
d'être est de comparer des filiales entre elles — d'où cette table explicite
plutôt qu'une conversion calculée, et d'où `tools/verify_gdelt_countries.py`
qui la confronte à l'API réelle.

Vérifiés en direct contre l'API GDELT le 4 août 2026 : ZA→SF, KE→KE. Les
autres viennent de la table FIPS 10-4 ; lancer l'outil de vérification pour
les confirmer un par un (l'API impose une requête toutes les 6 secondes).
"""

#: iso2 → (nom français, nom anglais, code FIPS GDELT)
COUNTRIES: dict[str, tuple[str, str, str]] = {
    "AO": ("Angola",                     "Angola",                   "AO"),
    "BF": ("Burkina Faso",               "Burkina Faso",             "UV"),
    "BI": ("Burundi",                    "Burundi",                  "BY"),
    "BJ": ("Bénin",                      "Benin",                    "BN"),
    "BW": ("Botswana",                   "Botswana",                 "BC"),
    "CD": ("RD Congo",                   "DR Congo",                 "CG"),
    "CF": ("Centrafrique",               "Central African Republic", "CT"),
    "CG": ("Congo",                      "Congo",                    "CF"),
    "CI": ("Côte d'Ivoire",              "Ivory Coast",              "IV"),
    "CM": ("Cameroun",                   "Cameroon",                 "CM"),
    "CV": ("Cap-Vert",                   "Cape Verde",               "CV"),
    "DJ": ("Djibouti",                   "Djibouti",                 "DJ"),
    "DZ": ("Algérie",                    "Algeria",                  "AG"),
    "EG": ("Égypte",                     "Egypt",                    "EG"),
    "ER": ("Érythrée",                   "Eritrea",                  "ER"),
    "ET": ("Éthiopie",                   "Ethiopia",                 "ET"),
    "GA": ("Gabon",                      "Gabon",                    "GB"),
    "GH": ("Ghana",                      "Ghana",                    "GH"),
    "GM": ("Gambie",                     "Gambia",                   "GA"),
    "GN": ("Guinée",                     "Guinea",                   "GV"),
    "GQ": ("Guinée équatoriale",         "Equatorial Guinea",        "EK"),
    "GW": ("Guinée-Bissau",              "Guinea-Bissau",            "PU"),
    "KE": ("Kenya",                      "Kenya",                    "KE"),
    "KM": ("Comores",                    "Comoros",                  "CN"),
    "LR": ("Libéria",                    "Liberia",                  "LI"),
    "LS": ("Lesotho",                    "Lesotho",                  "LT"),
    "LY": ("Libye",                      "Libya",                    "LY"),
    "MA": ("Maroc",                      "Morocco",                  "MO"),
    "MG": ("Madagascar",                 "Madagascar",               "MA"),
    "ML": ("Mali",                       "Mali",                     "ML"),
    "MR": ("Mauritanie",                 "Mauritania",               "MR"),
    "MU": ("Maurice",                    "Mauritius",                "MP"),
    "MW": ("Malawi",                     "Malawi",                   "MI"),
    "MZ": ("Mozambique",                 "Mozambique",               "MZ"),
    "NA": ("Namibie",                    "Namibia",                  "WA"),
    "NE": ("Niger",                      "Niger",                    "NG"),
    "NG": ("Nigeria",                    "Nigeria",                  "NI"),
    "RW": ("Rwanda",                     "Rwanda",                   "RW"),
    "SC": ("Seychelles",                 "Seychelles",               "SE"),
    "SD": ("Soudan",                     "Sudan",                    "SU"),
    "SL": ("Sierra Leone",               "Sierra Leone",             "SL"),
    "SN": ("Sénégal",                    "Senegal",                  "SG"),
    "SO": ("Somalie",                    "Somalia",                  "SO"),
    "SS": ("Soudan du Sud",              "South Sudan",              "OD"),
    "ST": ("Sao Tomé-et-Principe",       "Sao Tome and Principe",    "TP"),
    "SZ": ("Eswatini",                   "Eswatini",                 "WZ"),
    "TD": ("Tchad",                      "Chad",                     "CD"),
    "TG": ("Togo",                       "Togo",                     "TO"),
    "TN": ("Tunisie",                    "Tunisia",                  "TS"),
    "TZ": ("Tanzanie",                   "Tanzania",                 "TZ"),
    "UG": ("Ouganda",                    "Uganda",                   "UG"),
    "ZA": ("Afrique du Sud",             "South Africa",             "SF"),
    "ZM": ("Zambie",                     "Zambia",                   "ZA"),
    "ZW": ("Zimbabwe",                   "Zimbabwe",                 "ZI"),
}


def fips_code(iso2: str) -> str | None:
    """Code pays GDELT pour un ISO2. None si inconnu.

    None n'est PAS une erreur : l'appelant doit alors interroger GDELT sans
    filtre pays plutôt que d'inventer un code. Un code inventé ramènerait les
    articles d'un pays au hasard, ce qui est bien pire qu'une requête large.
    """
    entry = COUNTRIES.get((iso2 or "").upper())
    return entry[2] if entry else None


def country_names(iso2: str) -> list[str]:
    """Noms français et anglais d'un pays, pour la reconnaissance dans un texte."""
    entry = COUNTRIES.get((iso2 or "").upper())
    if not entry:
        return []
    return list(dict.fromkeys([entry[0], entry[1]]))
