"""Cadrage des titres, analyses croisées : (1) même événement, (2) par thème, (3) selon le lieu (part de quartiers prioritaires).

Lecture seule sur history.db. Mesure descriptive : parts de titres contenant des mots d'une famille (voir cadrage.py)."""
import itertools
import json
import math
import re
import sqlite3
import unicodedata
from collections import defaultdict

import cadrage as C

MIN_EV_TITLES = 2   # titres minimum d'un type de média dans un événement pour qu'il compte dans la comparaison
MIN_EVENTS = 12     # événements minimum pour publier un écart entre deux types
MIN_POP = 20000     # lieux : communes d'au moins 20 000 habitants (les communes à quartiers prioritaires sont grandes)
BUCKETS = [("aucun", "Aucun quartier prioritaire"), ("lt10", "Moins de 10 % des habitants en QPV"), ("10-25", "10 à 25 % en QPV"), ("gt25", "25 % et plus en QPV")]


def _name(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if unicodedata.category(c) != "Mn"))


def _cell():
    return {"n": 0, "fam": defaultdict(int)}


def _add(cell, fams):
    cell["n"] += 1
    for f in fams:
        cell["fam"][f] += 1


def load(db_path):
    c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=30)
    try:
        rows = c.execute("SELECT day,src,title,nom,dep,pop,themes FROM articles").fetchall()
    finally:
        c.close()
    seen, out = set(), []
    for day, src, title, nom, dep, pop, themes in rows:
        k = C._norm(title)
        if not title or k in seen:
            continue
        seen.add(k)
        t = C.SRC2T.get(C.nm(src))
        if not t:
            continue
        try:
            th = json.loads(themes or "[]")
        except Exception:
            th = []
        out.append({"day": day, "t": t, "nom": nom, "dep": dep, "pop": pop or 0, "th": th,
                    "fams": [f for f, rx in C.FAMRE.items() if rx.search(title)]})
    return out


def paired(rows):
    """Écart entre deux types de médias sur les mêmes événements (même commune, même jour)."""
    ev = defaultdict(lambda: defaultdict(_cell))
    glob = defaultdict(_cell)
    for r in rows:
        _add(glob[r["t"]], r["fams"])
        if r["nom"]:
            _add(ev[(r["nom"], r["dep"], r["day"])][r["t"]], r["fams"])
    pairs = {}
    for a, b in itertools.combinations(C.TYPE_ORDER, 2):
        items = []
        for byt in ev.values():
            ca, cb = byt.get(a), byt.get(b)
            if ca and cb and ca["n"] >= MIN_EV_TITLES and cb["n"] >= MIN_EV_TITLES:
                items.append((ca, cb))
        if len(items) < MIN_EVENTS:
            continue
        res = {"events": len(items), "titles_a": sum(x[0]["n"] for x in items), "titles_b": sum(x[1]["n"] for x in items), "fam": {}, "naive": {}}
        for f in C.FAMILIES:
            ws, ds = [], []
            for ca, cb in items:
                w = ca["n"] * cb["n"] / float(ca["n"] + cb["n"])
                ws.append(w)
                ds.append(100.0 * ca["fam"].get(f, 0) / ca["n"] - 100.0 * cb["fam"].get(f, 0) / cb["n"])
            sw = sum(ws)
            mean = sum(w * d for w, d in zip(ws, ds)) / sw
            se = math.sqrt(sum((w * (d - mean)) ** 2 for w, d in zip(ws, ds))) / sw
            res["fam"][f] = (mean, se)
            ga, gb = glob[a], glob[b]
            res["naive"][f] = 100.0 * ga["fam"].get(f, 0) / ga["n"] - 100.0 * gb["fam"].get(f, 0) / gb["n"] if ga["n"] and gb["n"] else None
        pairs[(a, b)] = res
    n_ev = sum(1 for byt in ev.values() if sum(1 for c_ in byt.values() if c_["n"] >= MIN_EV_TITLES) >= 2)
    return pairs, n_ev, len(ev)


def by_theme(rows, min_total=300):
    tot = defaultdict(int)
    for r in rows:
        for t in r["th"]:
            tot[t] += 1
    keep = [t for t, n in sorted(tot.items(), key=lambda x: -x[1]) if n >= min_total]
    out = {t: defaultdict(_cell) for t in keep}
    for r in rows:
        for t in r["th"]:
            if t in out:
                _add(out[t][r["t"]], r["fams"])
    return keep, out, tot


def by_place(rows, qpv_path):
    try:
        q = json.load(open(qpv_path, encoding="utf-8"))
    except Exception:
        return None
    part = {(_name(x["nom"]), str(x["dep"])): x["part"] for x in q}

    def bucket(r):
        p = part.get((_name(r["nom"]), str(r["dep"])))
        if p is None:
            return "aucun"
        return "lt10" if p < 10 else "10-25" if p < 25 else "gt25"

    res = {}
    for scope, flt in (("tous", lambda r: True), ("lyceens", lambda r: "lyceens" in r["th"])):
        g = {b: defaultdict(_cell) for b, _ in BUCKETS}
        com = {b: defaultdict(int) for b, _ in BUCKETS}
        for r in rows:
            if not r["nom"] or r["pop"] < MIN_POP or not flt(r):
                continue
            b = bucket(r)
            _add(g[b][r["t"]], r["fams"])
            _add(g[b]["ALL"], r["fams"])
            com[b][r["nom"]] += 1
        for b, _ in BUCKETS:  # concentration : combien de communes, et part de la plus citée
            tot = sum(com[b].values())
            top = max(com[b].items(), key=lambda x: x[1]) if com[b] else ("", 0)
            g[b]["ALL"]["communes"] = len(com[b])
            g[b]["ALL"]["top"] = (top[0], 100.0 * top[1] / tot if tot else 0.0)
        res[scope] = g
    return res


def compute_extra(db_path, qpv_path):
    rows = load(db_path)
    pairs, n_ev, n_loc = paired(rows)
    keep, th, tot = by_theme(rows)
    return {"pairs": pairs, "n_events": n_ev, "n_places_days": n_loc, "themes": keep, "theme_cells": th, "theme_tot": tot,
            "place": by_place(rows, qpv_path), "n_rows": len(rows)}


if __name__ == "__main__":
    import sys
    X = compute_extra(sys.argv[1], sys.argv[2])
    print("lignes", X["n_rows"], "événements multi-types", X["n_events"], "/", X["n_places_days"])
    for (a, b), r in X["pairs"].items():
        print("\n%s vs %s : %d événements, %d / %d titres" % (a, b, r["events"], r["titles_a"], r["titles_b"]))
        for f in ("desordre", "autorite", "voix", "victimes"):
            m, se = r["fam"][f]
            print("   %-9s écart même événement %+5.1f (±%.1f)   brut %+5.1f" % (f, m, 1.96 * se, r["naive"][f] if r["naive"][f] is not None else float("nan")))
    print("\nThèmes", X["themes"], {t: X["theme_tot"][t] for t in X["themes"]})
    if X["place"]:
        for scope, g in X["place"].items():
            print("\nLieux (%s)" % scope)
            for b, lab in BUCKETS:
                cell = g[b]["ALL"]
                n = cell["n"]
                print("  %-36s %5d titres | " % (lab, n) + " | ".join("%s %4.1f%%" % (f[:5], 100.0 * cell["fam"].get(f, 0) / n if n else 0) for f in ("desordre", "autorite", "voix", "victimes")))
