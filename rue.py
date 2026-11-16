"""Morts de la rue : décès de personnes sans chez-soi signalés au Collectif Les Morts de la Rue
(tableau public https://mortsdelarue.org/listes/, dernière année complète publiée).
L'identité des personnes n'est jamais reprise : âge, date et commune seulement."""
import html
import re
import time
import urllib.request

import travail

URL = "https://mortsdelarue.org/listes/"
UA = {"User-Agent": "ca-brule/1.0 (+https://oraclemarin.fr/cabrule/)"}
MOIS = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
        "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12, "décembre": 12}


def fetch(full, norm):
    t = urllib.request.urlopen(urllib.request.Request(URL, headers=UA), timeout=60).read().decode("utf-8", "replace")
    tb = re.search(r"(?s)<table.*?</table>", t)
    if not tb:
        return []
    rows = []
    for r in re.findall(r"(?s)<tr.*?</tr>", tb.group(0)):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in re.findall(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>", r)]
        rows.append(cells)
    head = [norm(h) for h in rows[0]]
    col = {k: i for i, k in enumerate(head)}
    need = ("age", "jour", "mois", "annee", "commune de deces", "departement de deces")
    if not all(k in col for k in need):
        raise ValueError("format du tableau modifié : %s" % head)
    locs, items = {}, []
    for r in rows[1:]:
        if len(r) < len(head):
            continue
        year = int(r[col["annee"]]) if r[col["annee"]].isdigit() else None
        month = MOIS.get(r[col["mois"]].lower().strip())
        if not year:
            continue
        day = int(r[col["jour"]]) if r[col["jour"]].isdigit() and 1 <= int(r[col["jour"]]) <= 31 else 15
        ts = time.mktime((year, month or 7, day, 12, 0, 0, 0, 0, -1))
        dep = r[col["departement de deces"]].strip()
        dep = dep.zfill(2) if dep.isdigit() and len(dep) < 2 else dep
        com = re.sub(r"-\s*\w{1,3}$", "", r[col["commune de deces"]]).strip()
        com = re.sub(r"\s+\d{1,2}\s*(?:E|ER|EME|ÈME)$", "", com, flags=re.I)  # « Paris 15E » -> Paris
        key = (com, dep)
        if key not in locs:
            locs[key] = travail.locate(com.title(), dep, full, norm) if com else None
        c = locs[key]
        age = r[col["age"]] if r[col["age"]].isdigit() and r[col["age"]] != "0" else ""
        items.append({"ts": ts, "year": year, "lat": c["lat"] if c else None, "lon": c["lon"] if c else None,
                      "place": c["nom"] if c else com.title(), "dep": c["dep"] if c else dep,
                      "age": (age + " ans") if age else "", "dated": bool(month and day != 15 or r[col["jour"]] == "15"),
                      "text": "Personne sans chez-soi%s décédée%s." % (", " + age + " ans," if age else "",
                                                                        " en " + r[col["mois"]] + " " + str(year) if month else " en " + str(year)),
                      "url": URL})
    return items
