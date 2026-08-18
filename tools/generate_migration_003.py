"""
Génère migrations/003_extend_operators.sql à partir de config/operators.json.

La migration est GÉNÉRÉE plutôt qu'écrite à la main pour garantir que les
alias de dim_subsidiary correspondent exactement aux `subsidiary_name` publiés
par les collecteurs : c'est ce qui permet le rattachement automatique d'un avis
à sa filiale. Un alias qui diverge d'un caractère laisse subsidiary_id à NULL
et l'avis disparaît de tous les agrégats par pays/opérateur.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config" / "operators.json"
OUT = ROOT / "migrations" / "003_extend_operators.sql"

# iso3 + région par pays présent dans la config.
COUNTRY_META = {
    "BJ": ("BEN", "Afrique de l'Ouest"), "BF": ("BFA", "Afrique de l'Ouest"),
    "ML": ("MLI", "Afrique de l'Ouest"), "CF": ("CAF", "Afrique centrale"),
    "SN": ("SEN", "Afrique de l'Ouest"), "CI": ("CIV", "Afrique de l'Ouest"),
    "GN": ("GIN", "Afrique de l'Ouest"), "GW": ("GNB", "Afrique de l'Ouest"),
    "SL": ("SLE", "Afrique de l'Ouest"), "LR": ("LBR", "Afrique de l'Ouest"),
    "TG": ("TGO", "Afrique de l'Ouest"), "NE": ("NER", "Afrique de l'Ouest"),
    "NG": ("NGA", "Afrique de l'Ouest"), "GH": ("GHA", "Afrique de l'Ouest"),
    "CM": ("CMR", "Afrique centrale"), "CD": ("COD", "Afrique centrale"),
    "CG": ("COG", "Afrique centrale"), "GA": ("GAB", "Afrique centrale"),
    "TD": ("TCD", "Afrique centrale"), "MG": ("MDG", "Afrique de l'Est"),
    "KE": ("KEN", "Afrique de l'Est"), "TZ": ("TZA", "Afrique de l'Est"),
    "UG": ("UGA", "Afrique de l'Est"), "RW": ("RWA", "Afrique de l'Est"),
    "ET": ("ETH", "Afrique de l'Est"), "SC": ("SYC", "Afrique de l'Est"),
    "SD": ("SDN", "Afrique du Nord"), "SS": ("SSD", "Afrique de l'Est"),
    "EG": ("EGY", "Afrique du Nord"), "MA": ("MAR", "Afrique du Nord"),
    "TN": ("TUN", "Afrique du Nord"), "DZ": ("DZA", "Afrique du Nord"),
    "ZA": ("ZAF", "Afrique australe"), "ZM": ("ZMB", "Afrique australe"),
    "MW": ("MWI", "Afrique australe"), "MZ": ("MOZ", "Afrique australe"),
    "LS": ("LSO", "Afrique australe"), "SZ": ("SWZ", "Afrique australe"),
    # Extension à l'ensemble du continent : 16 pays ajoutés pour que la carte
    # cesse d'afficher des zones grises non couvertes.
    #
    # Le Sahara occidental (EH) est délibérément ABSENT : territoire disputé,
    # desservi par les opérateurs marocains déjà suivis, sans opérateur propre.
    # L'y déclarer reviendrait à trancher une question de souveraineté, ce qui
    # n'est pas le rôle de ce fichier.
    "AO": ("AGO", "Afrique australe"), "BW": ("BWA", "Afrique australe"),
    "NA": ("NAM", "Afrique australe"), "ZW": ("ZWE", "Afrique australe"),
    "MU": ("MUS", "Afrique de l'Est"), "KM": ("COM", "Afrique de l'Est"),
    "DJ": ("DJI", "Afrique de l'Est"), "SO": ("SOM", "Afrique de l'Est"),
    "ER": ("ERI", "Afrique de l'Est"), "BI": ("BDI", "Afrique de l'Est"),
    "GM": ("GMB", "Afrique de l'Ouest"), "CV": ("CPV", "Afrique de l'Ouest"),
    "MR": ("MRT", "Afrique de l'Ouest"), "GQ": ("GNQ", "Afrique centrale"),
    "ST": ("STP", "Afrique centrale"), "LY": ("LBY", "Afrique du Nord"),
}

# code interne + groupe parent + anciens noms, par opérateur.
OPERATOR_META = {
    "Moov Africa": ("moov_africa", "Maroc Telecom", ["Etisalat", "Telecel"]),
    "Orange": ("orange", "Orange S.A.", ["Sonatel"]),
    "MTN": ("mtn", "MTN Group", []),
    "Airtel": ("airtel", "Bharti Airtel", ["Zain Africa", "Celtel"]),
    "Vodacom": ("vodacom", "Vodafone Group", []),
    "Vodafone": ("vodafone", "Vodafone Group", []),
    "Safaricom": ("safaricom", "Vodacom / Vodafone", []),
    "Zain": ("zain", "Zain Group", []),
    "Telkom": ("telkom", None, []),
    "Ooredoo": ("ooredoo", "Ooredoo Group", []),
    "Camtel": ("camtel", None, []),
    # Opérateurs nationaux ajoutés pour que chaque pays affiche sa concurrence
    # réelle : 13 pays n'avaient qu'un seul opérateur suivi.
    "Maroc Telecom": ("maroc_telecom", "e& (Etisalat Group)", ["IAM"]),
    "Inwi": ("inwi", "Al Mada", ["Wana"]),
    "Mobilis": ("mobilis", "Algérie Télécom", ["ATM"]),
    "Djezzy": ("djezzy", "Veon", ["Orascom Telecom"]),
    "Free Sénégal": ("free_senegal", "Axian", ["Tigo Sénégal"]),
    "Expresso": ("expresso", "Sudatel", []),
    "Ethio Telecom": ("ethio_telecom", None, []),
    "Togocom": ("togocom", "Axian", ["Togo Telecom", "Togo Cellulaire"]),
    "Africell": ("africell", "Africell Holding", []),
    "TNM": ("tnm", None, ["Telekom Networks Malawi"]),
    "Tmcel": ("tmcel", None, ["mCel", "Telecomunicações de Moçambique"]),
    "Movitel": ("movitel", "Viettel", []),
    "Econet": ("econet", "Econet Wireless", []),
    "Eswatini Mobile": ("eswatini_mobile", None, []),
    "Cable & Wireless": ("cable_wireless", "Cable & Wireless", []),
    "Telecel": ("telecel", "Telecel Group", []),
    "Sotel Tchad": ("sotel_tchad", None, ["Salam"]),
    "Glo": ("glo", "Globacom", []),
    "9mobile": ("nine_mobile", None, ["Etisalat Nigeria"]),
    "AirtelTigo": ("airteltigo", None, ["Tigo Ghana"]),
    "Cell C": ("cell_c", None, []),
    "Telma": ("telma", "Axian", []),
    "Halotel": ("halotel", "Viettel", []),
    "Celtiis": ("celtiis", "Bénin Telecoms", []),
    # Opérateurs des 16 pays ajoutés. Noms et groupes parents relèvent de
    # connaissances publiques ; les IDENTIFIANTS de collecte, eux, restent à
    # null et ne seront renseignés qu'après vérification contre les vraies
    # boutiques (tools/verify_identifiers.py).
    "Unitel": ("unitel", None, []),
    "Movicel": ("movicel", None, []),
    "Mascom": ("mascom", "Mascom Wireless", []),
    "BTC": ("btc", "Botswana Telecommunications", ["beMobile"]),
    "MTC": ("mtc", "Mobile Telecommunications Ltd", []),
    "TN Mobile": ("tn_mobile", "Telecom Namibia", ["Leo"]),
    "NetOne": ("netone", None, []),
    "Emtel": ("emtel", None, []),
    "Mauritius Telecom": ("mauritius_telecom", None, ["my.t", "Orange Mauritius"]),
    "Comores Telecom": ("comores_telecom", None, ["Huri"]),
    "Djibouti Telecom": ("djibouti_telecom", None, ["Evatis"]),
    "Hormuud": ("hormuud", "Hormuud Telecom", []),
    "Somtel": ("somtel", "Dahabshiil Group", []),
    "Telesom": ("telesom", None, []),
    "EriTel": ("eritel", None, ["Eritrea Telecommunication"]),
    "Lumitel": ("lumitel", "Viettel", []),
    "Onatel Burundi": ("onatel_burundi", "Maroc Telecom", ["Smart Burundi"]),
    "QCell": ("qcell", None, []),
    "Gamcel": ("gamcel", "Gamtel", []),
    "CVMovel": ("cvmovel", "Cabo Verde Telecom", []),
    "Mauritel": ("mauritel", "Maroc Telecom", []),
    "Chinguitel": ("chinguitel", "Sudatel", []),
    "Mattel": ("mattel", "Tunisie Telecom", []),
    "GETESA": ("getesa", "Orange S.A.", ["Orange Guinée équatoriale"]),
    "CST": ("cst", "Companhia Santomense de Telecomunicações", []),
    "Libyana": ("libyana", "Libyan Post Telecom", []),
    "Almadar": ("almadar", "Libyan Post Telecom", ["Almadar Aljadid"]),
    # Égypte, ajoutés le 2026-08-05 après validation auprès de la NTRA, qui
    # nomme quatre titulaires quand le périmètre n'en déclarait que deux (voir
    # config/regulators.json). Les deux marques ont changé de nom récemment,
    # d'où les alias : la presse écrit encore « Etisalat Misr », et « WE » seul
    # est trop court pour identifier quoi que ce soit dans un article.
    "e&": ("e_and", "e& (Emirates Telecommunications Group)",
           ["Etisalat", "Etisalat Egypt", "Etisalat Misr", "e& Egypt"]),
    "WE": ("we", "Telecom Egypt", ["Telecom Egypt", "WE Telecom Egypt"]),
    # Zambie, ajouté le 2026-08-06. Opérateur public, troisième réseau mobile
    # du pays (MNC 645-03), absent du périmètre depuis l'origine. Identifiants
    # d'application vérifiés sur les boutiques zambiennes ; la liste de licences
    # de ZICTA n'a en revanche PAS pu être consultée (registre derrière une
    # application JavaScript sans API), voir config/regulators.json.
    "Zamtel": ("zamtel", "Zambia Telecommunications Company",
               ["Zamtel Zambia", "Zambia Telecommunications"]),
}


def q(value: str) -> str:
    """Littéral SQL échappé."""
    return "'" + value.replace("'", "''") + "'"


def arr(values: list[str]) -> str:
    inner = ", ".join(q(v) for v in values)
    return f"ARRAY[{inner}]::text[]" if values else "ARRAY[]::text[]"


def main() -> None:
    subs = json.loads(CONFIG.read_text(encoding="utf-8"))["subsidiaries"]

    countries, operators = {}, {}
    for s in subs:
        countries[s["iso2"]] = s["country"]
        operators[s["operator"]] = True

    missing = [c for c in countries if c not in COUNTRY_META]
    if missing:
        raise SystemExit(f"Pays sans métadonnées iso3/région : {missing}")
    unknown = [o for o in operators if o not in OPERATOR_META]
    if unknown:
        raise SystemExit(f"Opérateurs sans métadonnées : {unknown}")

    lines = [
        "-- =========================================================================",
        "-- 003 — Extension du périmètre : opérateurs télécoms africains",
        "--",
        "-- FICHIER GÉNÉRÉ par tools/generate_migration_003.py depuis",
        "-- config/operators.json. Ne pas éditer à la main : régénérer après toute",
        "-- modification de la configuration, sinon les alias divergent des noms",
        "-- publiés par les collecteurs et les avis cessent de se rattacher.",
        "--",
        "-- Idempotent (ON CONFLICT DO NOTHING) : rejouable sans effet de bord.",
        "-- =========================================================================",
        "",
        "BEGIN;",
        "",
        "-- Pays ---------------------------------------------------------------",
        "INSERT INTO dim_country (iso2, iso3, name, region) VALUES",
    ]

    rows = [
        f"    ({q(iso2)}, {q(COUNTRY_META[iso2][0])}, {q(name)}, {q(COUNTRY_META[iso2][1])})"
        for iso2, name in sorted(countries.items())
    ]
    lines.append(",\n".join(rows))
    lines += ["ON CONFLICT (iso2) DO NOTHING;", ""]

    lines += ["-- Opérateurs ---------------------------------------------------------",
              "INSERT INTO dim_operator (code, name, parent_group, former_names) VALUES"]
    rows = []
    for op in sorted(operators):
        code, parent, former = OPERATOR_META[op]
        parent_sql = q(parent) if parent else "NULL"
        rows.append(f"    ({q(code)}, {q(op)}, {parent_sql}, {arr(former)})")
    lines.append(",\n".join(rows))
    lines += ["ON CONFLICT (code) DO NOTHING;", ""]

    lines += [
        "-- Filiales -----------------------------------------------------------",
        "--   aliases = les noms exacts publiés par les collecteurs, d'où le",
        "--   rattachement automatique de reviews.company -> subsidiary_id.",
        "INSERT INTO dim_subsidiary (operator_id, country_id, name, aliases)",
        "SELECT o.operator_id, c.country_id, v.name, v.aliases",
        "FROM (VALUES",
    ]
    rows = []
    for s in subs:
        code = OPERATOR_META[s["operator"]][0]
        # Tout nom qu'un collecteur peut publier dans reviews.company doit
        # figurer ici. Le terme de recherche RSS en fait partie : il est
        # souvent anglicisé ("MTN Uganda" ramène plus d'articles que
        # "MTN Ouganda") et diffère donc du nom de la filiale.
        aliases = {s["subsidiary_name"], f"{s['operator']} {s['country']}"}
        rss = (s.get("sources") or {}).get("rss") or {}
        if rss.get("search_term"):
            aliases.add(rss["search_term"])
        rows.append(
            f"        ({q(code)}, {q(s['iso2'])}, {q(s['subsidiary_name'])}, "
            f"{arr(sorted(aliases))})"
        )
    lines.append(",\n".join(rows))
    lines += [
        "     ) AS v(op_code, iso2, name, aliases)",
        "JOIN dim_country  c ON c.iso2 = v.iso2",
        "JOIN dim_operator o ON o.code = v.op_code",
        "-- Fusion des alias plutôt que DO NOTHING : la migration doit rester",
        "-- rejouable après un ajout d'alias, sans perdre ceux déjà en base",
        "-- (ex. 'Etisalat Bénin', hérité de l'ancien nom de marque).",
        "ON CONFLICT (operator_id, country_id) DO UPDATE",
        "SET aliases = ARRAY(",
        "    SELECT DISTINCT unnest(dim_subsidiary.aliases || EXCLUDED.aliases)",
        ");",
        "",
        "COMMIT;",
        "",
    ]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Écrit : {OUT}")
    print(f"  {len(countries)} pays, {len(operators)} opérateurs, {len(subs)} filiales")


if __name__ == "__main__":
    main()
