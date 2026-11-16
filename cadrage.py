"""Cadrage des titres : vocabulaire employé par type de média, global et jour par jour.

Mesure descriptive : on compte la part des titres qui contiennent certains mots. Aucun jugement sur les médias ni sur les faits.
Utilisé par app.py (page /cadrage) et par les analyses ponctuelles. Lecture seule sur history.db.
"""
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict


def nm(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"\.(fr|com|info)$", "", s.strip())
    s = re.sub(r"^www\.", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


# type de média -> noms normalisés des sources (voir nm)
TYPES = {
    "Presse nationale": "lefigaro lemonde liberation leparisien lhumanite 20minutes revolutionpermanente",
    "TV et radio d'info privées": "bfmtv bfm cnews europe1 tf1info jeanmarcmorandini rmc rtl",
    "Service public": "franceinfo france3regions ici outremerla1ere france24",
    "Presse régionale": ("ouestfrance letelegramme ledauphinelibere sudouest lestrepublicain nicematin leprogres midilibre lavoixdunord lindependant "
                         "ladepeche lamontagne lanouvellerepublique laprovence parisnormandie dernieresnouvellesdalsace dna lebienpublic nordlittoral lunion "
                         "lerepublicainlorrain vosgesmatin lejournaldesaoneetloire lejsl lalsace courrierpicard charentelibre lepopulaireducentre "
                         "leberryrepublicain larepubliqueducentre lechorepublicain lasemainedespyrenees larepubliquedespyrenees lesteclair "
                         "lamarseillaise lereveildemidi"),
    "Sites d'infos locales": "actu lyoncapitale lyonmag myangers angersinfo tendanceouest maville dijonactualites macommune infosmedianantes mesinfos maritima sortiraparis",
}
SRC2T = {n: t for t, names in TYPES.items() for n in names.split()}
TYPE_ORDER = ["Presse nationale", "TV et radio d'info privées", "Service public", "Presse régionale", "Sites d'infos locales"]
MIN_CELL = 25  # en dessous, une case n'est pas affichée (échantillon trop petit)

# famille -> sous-catégories (nom lisible -> expression)
FAMILIES = {
    "desordre": ("Le désordre", "casseurs, émeutes, incendies, dégâts, gaz lacrymogène…", {
        "casseurs / émeutiers / voyous": r"casseur|[ée]meut|black bloc|voyou|vandal|pilleur|pillage",
        "délinquance / violences urbaines / hors de contrôle": r"d[ée]linquan|violences urbaines|hors de contr|sauvage",
        "illégitimité (illégal, radicaux, ultras)": r"ill[ée]gal|radicaux|ultras?\b",
        "heurts / affrontements": r"heurts|affrontement|[ée]chauffour",
        "jets / projectiles / mortiers": r"caillass|jets? de|mortier|projectiles?",
        "incendies / feux": r"incendi|(?<!couvre-)\bfeu\b|\bfeux\b|br[uû]l[ée]s?\b|br[uû]lent",
        "dégâts / dégradations / saccages": r"d[ée]g[âa]ts|d[ée]gradation|saccag",
        "gaz lacrymogène": r"lacrymo|\bgaz\b",
        "violences / tensions / chaos / débordements": r"(?<!aucun )(?<!aucune )(?<!sans )(?<!pas de )(?<!plus de )(?<!contre les )(?<!contre )violences?(?! polici| conjug| sexist| sexuel| faites)|(?<!aucun )(?<!aucune )(?<!sans )(?<!pas de )(?<!plus de )\btensions?\b|chaos|(?<!aucun )(?<!aucune )(?<!sans )(?<!pas de )(?<!plus de )d[ée]bordement",
        "agressions / attaques / tirs": r"agress|attaqu|\btirs?\b|poubelles?",
    }),
    "autorite": ("Les autorités", "police, préfet, gouvernement, interdictions…", {
        "police / gendarmerie / CRS": r"(?<!anti-)(?<!anti )(?:police|polici)|gendarm|\bcrs\b|forces de l.ordre",
        "sécurité / surveillance / autorisation": r"mesures? .{0,20}s[ée]curit|p[ée]rim[eè]tre|dispositif|\bdrone|surveill|autoris[ée]|non d[ée]clar|r[ée]quisition",
        "préfet / préfecture / interdiction": r"pr[ée]f[eèé][ct]|interdit|arr[êe]t[ée] pr|interdiction|couvre-feu|commissariat",
        "gouvernement / ministre": r"ministre|gouvernement|premier ministre|[ée]lys[ée]e|macron",
    }),
    "suites": ("Après coup : arrestations, blessés, justice", "interpellations, garde à vue, blessés, procès…", {
        "interpellations / garde à vue": r"interpell|garde[s]? [àa] vue",
        "blessés": r"\bbless",
        "justice (déféré, condamné, procès)": r"d[ée]f[ée]r|condamn|comparution|proc[ée]s|tribunal",
    }),
    "voix": ("La voix des manifestants", "colère, revendications, dénonciations, soutien…", {
        "colère": r"col[eè]re|ras-le-bol",
        "revendications (exigent, réclament)": r"revendic|exige|r[ée]clam",
        "dénonciation": r"d[ée]nonc|accus",
        "soutien / solidarité / hommage": r"solidarit|soutien|hommage",
        "appel / lutte / revendications sociales": r"\bappel|\blutte|\bcombat|salaires?|pouvoir d.achat|vie ch[èe]re|aust[ée]rit|maintenir la pression",
    }),
    "action": ("Le mouvement lui-même", "cortège, rassemblement, blocus, grève, mobilisation…", {
        "cortège / défilé": r"cort[eè]ge|d[ée]fil[ée]",
        "rassemblement": r"rassembl",
        "blocus / blocage": r"blocus|blocage|bloqu",
        "grève / syndicat": r"gr[eè]ve|syndicat|intersyndicale",
        "mobilisation": r"mobilis",
    }),
    "victimes": ("Les manifestants blessés", "tirs de LBD, violences policières, jeunes blessés…", {
        "armes dites non létales (LBD, flashball, éborgné)": r"flashball|\blbd\b|[ée]borgn|grenade",
        "violences policières / brutalité": r"violences? polici|matraqu|brutalit|tabass",
        "jeune ou lycéen blessé": r"jeune.{0,25}bless|bless.{0,25}(lyc[ée]en|[ée]l[èe]ve|mineur)|hospitalis",
        "main arrachée / IGPN / plainte": r"main arrach|igpn|plainte",
        "blessures graves (poignardé, fracture, œil)": r"poignard|fractur[ée]|blessure [àa] l|plaqu[ée]e? au sol",
    }),
    "ampleur": ("Ampleur et calme", "des milliers de personnes, manifestation pacifique…", {
        "ampleur (milliers, massive, record)": r"des milliers|massive|record|nombreux|foule|\d{3,} (personnes|manifestants|lyc)",
        "chiffres de participation (centaines, environ, près de)": r"\bcentaines?\b|\benviron\b|pr[èe]s de \d|plusieurs (centaines|milliers)",
        "pacifique / calme / sans incident": r"pacifique|calme|sans incident",
    }),
    "parole": ("Parole rapportée", "selon, estime, déclare, affirme, le maire, les élus…", {
        "verbes de déclaration (selon, estime, déclare…)": r"\bselon\b|\bestim|d[ée]clare|affirme|\bassure[nt]*\b|\bindique|r[ée]agit",
        "maire / élus / ministre cité": r"\bmaire\b|\b[ée]lus?\b|nu[ñn]ez",
        "témoignages (témoigne, explique, souligne…)": r"t[ée]moign|se f[ée]licit|\bexplique[nt]*\b|souligne|regrette|d[ée]plore|pr[ée]cise",
    }),
    "pratique": ("Informations pratiques", "perturbations, circulation, métro, bus, lignes fermées, parcours…", {
        "transports / circulation / perturbations": r"perturb|circulation|m[ée]tro|\bbus\b|\btram|\blignes?\b|\bstations?\b|\bsncf\b|\btrains?\b",
        "fermetures / parcours / prévu": r"\bferm[ée]s?\b|parcours|pr[ée]vu|itin[ée]raire|\bhoraires?\b",
        "à quoi s'attendre / services touchés": r"[àa] quoi (s.)?attendre|ce qui (vous )?attend|ce que pr[ée]voit|[ée]coles? (ferm|sans)|garderie|cr[èe]ches?|a[ée]roports?|vols? annul|d[ée]parts? [àa] \d|r[ée]seau (de transport|urbain|tcl)|d[ée]parts?\b|d[ée]butera|\b\d{1,2} ?h ?\d{0,2}\b|m[ée]t[ée]o|suivez|d[ée]placements|reprise|impact sur le r[ée]seau",
    }),
    "politique": ("La politique", "extrême gauche, extrême droite, LFI, RN…", {
        "extrême gauche / LFI / Mélenchon": r"extr[êe]me gauche|ultra-?gauche|insoumis|\blfi\b|m[ée]lenchon",
        "extrême droite / RN": r"extr[êe]me droite|\brn\b|bardella|le pen",
    }),
}
# familles montrées dans la partie simple (la « politique » reste dans la partie détaillée)
SIMPLE = ["desordre", "autorite", "voix", "victimes", "parole", "pratique"]

SUBS = [(f, k, re.compile(v, re.I)) for f, (_, _, d) in FAMILIES.items() for k, v in d.items()]
FAMRE = {f: re.compile("|".join("(?:%s)" % v for v in d.values()), re.I) for f, (_, _, d) in FAMILIES.items()}


def _norm(t):
    t = unicodedata.normalize("NFD", (t or "").lower())
    return re.sub(r"[^a-z0-9 ]", "", "".join(c for c in t if unicodedata.category(c) != "Mn"))[:70]


def _cell():
    return {"n": 0, "fam": defaultdict(int), "sub": defaultdict(int)}


def compute(db_path, top_days=8, min_day_titles=150, before=None):
    """Lit les articles (dédoublonnés par titre) ; renvoie les chiffres globaux, par jour et par média. Lecture seule."""
    c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=30)
    try:
        rows = c.execute("SELECT day,src,title FROM articles").fetchall()
    finally:
        c.close()
    seen = set()
    glob, day_t, day_n, med = defaultdict(_cell), defaultdict(lambda: defaultdict(_cell)), defaultdict(int), defaultdict(_cell)
    unk = defaultdict(int)
    for day, src, title in rows:
        k = _norm(title)
        if not title or k in seen:
            continue
        seen.add(k)
        t = SRC2T.get(nm(src))
        day_n[day] += 1
        if not t:
            unk[src] += 1
            continue
        fams = [f for f, rx in FAMRE.items() if rx.search(title)]
        subs = [(f, s) for f, s, rx in SUBS if rx.search(title)]
        med[(t, nm(src))]["label"] = src
        for cell in (glob[t], day_t[day][t], med[(t, nm(src))]):
            cell["n"] += 1
            for f in fams:
                cell["fam"][f] += 1
            for s in subs:
                cell["sub"][s] += 1
    big = sorted((d for d in day_n if day_n[d] >= min_day_titles and (before is None or (d or "") < before)), key=lambda d: -day_n[d])[:top_days]
    return {"glob": glob, "days": {d: day_t[d] for d in big}, "day_n": day_n, "big": sorted(big), "med": med,
            "classified": sum(v["n"] for v in glob.values()), "unclassified": sum(unk.values()),
            "unk_top": sorted(unk.items(), key=lambda x: -x[1])[:8], "total": len(seen), "at": time.time()}


if __name__ == "__main__":
    import sys
    R = compute(sys.argv[1] if len(sys.argv) > 1 else "/opt/ca-brule/history.db")
    print("classés", R["classified"], "non classés", R["unclassified"], R["unk_top"])
    for d in sorted(R["big"], key=lambda d: -R["day_n"][d]):
        print("\n== %s : %d titres uniques" % (d, R["day_n"][d]))
        print("%-28s %5s | " % ("type", "n") + " | ".join("%-9s" % FAMILIES[f][0][:9] for f in FAMILIES))
        for t in TYPE_ORDER:
            cell = R["days"][d].get(t)
            if not cell or cell["n"] < MIN_CELL:
                print("%-28s %5s |  (trop peu de titres)" % (t, cell["n"] if cell else 0))
                continue
            print("%-28s %5d | " % (t, cell["n"]) + " | ".join("%8.1f%%" % (100 * cell["fam"][f] / cell["n"]) for f in FAMILIES))
