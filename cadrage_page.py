"""Page /cadrage : le vocabulaire des titres de presse selon le type de média, en deux parties (simple, puis données complètes).

build(R, esc, base) -> (css, body_html) ; csv_text(R) -> str. R vient de cadrage.compute().
Aucun jugement : la page compte des mots dans des titres et affiche les effectifs et les marges d'erreur.
"""
import json
import math
import re
import time
from datetime import datetime

import cadrage as C
import cadrage_extra as CX

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
COUL = {"Presse nationale": "#ff5a1f", "TV et radio d'info privées": "#9b6bff", "Service public": "#38bdf8", "Presse régionale": "#17c3a2", "Sites d'infos locales": "#ffb020"}
DE = {"Presse nationale": "de la presse nationale", "TV et radio d'info privées": "des télés et radios d'info privées", "Service public": "du service public",
      "Presse régionale": "de la presse régionale", "Sites d'infos locales": "des sites d'infos locales"}
THE = {"Presse nationale": "la presse nationale", "TV et radio d'info privées": "les télés et radios d'info privées", "Service public": "le service public",
       "Presse régionale": "la presse régionale", "Sites d'infos locales": "les sites d'infos locales"}
FAMPH = {"parole": "des mots de parole rapportée (selon, estime, déclare, le maire…)", "pratique": "des mots d'informations pratiques (perturbations, métro, lignes, parcours…)", "desordre": "des mots de désordre (casseurs, incendies, dégâts…)", "autorite": "des mots d'autorité (police, préfet, gouvernement…)",
         "voix": "des mots de la voix des manifestants (colère, revendications…)", "victimes": "des mots sur les manifestants blessés (LBD, violences policières…)",
         "action": "des mots sur le mouvement lui-même (cortège, blocus, grève…)", "suites": "des mots d'après coup (interpellations, blessés, procès…)", "ampleur": "des mots d'ampleur ou de calme", "politique": "des mots de politique (extrême gauche, extrême droite…)"}

CSS = """
.cd h2{margin:34px 0 8px}.cd .fam{margin:26px 0;padding:18px 16px;background:var(--panel);border:1px solid var(--line);border-radius:16px}
.cd .fam h3{margin:0 0 2px;font-size:19px}.cd .fam .sub{margin:0 0 12px;color:var(--mute);font-size:13.5px}
.cd .bar{display:grid;grid-template-columns:200px 1fr 92px;gap:10px;align-items:center;margin:6px 0;font-size:13.5px;color:#c9ced6}
.cd .bar .tr{height:22px;background:#10131a;border:1px solid var(--line);border-radius:7px;overflow:hidden}.cd .bar .tr i{display:block;height:100%;border-radius:0 5px 5px 0}
.cd .bar b{color:#fff;text-align:right;font-size:14px}.cd .bar.nd b{color:var(--mute);font-weight:400;font-size:12px}
.cd .read{margin:12px 0 0;font-size:14.5px;color:#e6e9ee}.cd .read b{color:#fff}
.cd .dchips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.cd .dchip{cursor:pointer;border:1px solid var(--line);background:#10131a;color:#c9ced6;border-radius:999px;padding:7px 13px;font:inherit;font-size:13.5px}
.cd .dchip.on{background:var(--fire);border-color:var(--fire);color:#0b0d12;font-weight:700}
.cd .fpan{display:none}.cd .fpan.on{display:block}.cd .vs{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0}.cd .vs select{background:#10131a;color:#e6e9ee;border:1px solid var(--line);border-radius:10px;padding:8px 10px;font:inherit;font-size:14px;max-width:100%}.cd .vs span{color:var(--mute)}.cd .vsl{margin:4px 0;font-size:13px;color:#e6e9ee}.cd .vsl b{color:var(--fire)}
.cd .pan{display:none}.cd .pan.on{display:block}
.cd .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin:14px 0}.cd .cards>div{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 15px;font-size:14.5px}
.cd .cards h4{margin:0 0 6px;font-size:15.5px}
.cd .tw{overflow-x:auto;margin:10px 0 20px;border:1px solid var(--line);border-radius:12px}.cd table{border-collapse:collapse;width:100%;font-size:13px;min-width:640px}
.cd th,.cd td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}.cd th:first-child,.cd td:first-child{text-align:left;white-space:normal}
.cd thead th{background:#10131a;color:#c9ced6;position:sticky;top:0}.cd tr.g td{background:#12151c;color:#fff;font-weight:700;text-align:left}
.cd td small{color:var(--mute)}.cd code{font-size:11.5px;color:#c9ced6;white-space:normal;word-break:break-word}
.cd .kpi{display:flex;flex-wrap:wrap;gap:10px 22px;margin:12px 0;color:#c9ced6;font-size:14px}.cd .kpi b{color:#fff;font-size:20px;display:block}
.cd .lg{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:#c9ced6;margin:8px 0}.cd .lg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
@media(max-width:700px){.cd .bar{grid-template-columns:1fr 70px}.cd .bar span{grid-column:1/-1}}
"""


def f1(x):
    return ("%.1f" % x).replace(".", ",")


def date_fr(d):
    try:
        y, m, j = d.split("-")
        return "%d %s %s" % (int(j), MOIS[int(m) - 1], y)
    except Exception:
        return d or "?"


def pct(cell, kind, key):
    return 100.0 * (cell[kind].get(key, 0)) / cell["n"] if cell and cell["n"] else 0.0


def moe(p, n):
    """Marge d'erreur à 95 % (points de pourcentage), approximation binomiale."""
    return 196.0 * math.sqrt(max(p / 100.0 * (1 - p / 100.0), 0) / n) if n else 0.0


def ok(cell):
    return bool(cell) and cell["n"] >= C.MIN_CELL


def gap_sentence(cells, label):
    """Plus grand écart net (au-delà de l'incertitude) entre deux types de médias, sur les familles de la partie simple."""
    best = None
    for f in C.SIMPLE:
        L = [(pct(cells[t], "fam", f), cells[t]["n"], t) for t in C.TYPE_ORDER if ok(cells.get(t))]
        for a in L:
            for b in L:
                if a[0] <= b[0] or a[0] < 5:
                    continue
                se = math.sqrt(a[0] / 100 * (1 - a[0] / 100) / a[1] + b[0] / 100 * (1 - b[0] / 100) / b[1]) * 100
                z = (a[0] - b[0]) / se if se else 0
                if z >= 2.5 and (best is None or (a[0] - b[0]) > best[0]):
                    best = (a[0] - b[0], f, a, b)
    if not best:
        return "%s, aucun écart net entre les types de médias sur ces mots (les différences sont trop petites pour être sûres)." % label
    _, f, a, b = best
    return "%s, %s apparaissent dans <b>%d titres sur 100</b> %s, contre <b>%d sur 100</b> dans %s." % (label, FAMPH[f], round(a[0]), DE[a[2]], round(b[0]), THE[b[2]])


def bars(cells, fam, esc, scale=None, detail=False):
    vals = [pct(cells.get(t), "fam", fam) for t in C.TYPE_ORDER if ok(cells.get(t))]
    mx = scale or max(vals + [1.0]) * 1.15
    out = []
    for t in C.TYPE_ORDER:
        cell = cells.get(t)
        if not ok(cell):
            out.append('<div class="bar nd"><span>%s</span><div class="tr"></div><b>trop peu de titres</b></div>' % esc(t))
            continue
        p = pct(cell, "fam", fam)
        txt = ("%s %%" % f1(p)) if detail else ("%d sur 100" % round(p))
        out.append('<div class="bar"><span>%s <small style="color:var(--mute)">(%d titres)</small></span><div class="tr"><i style="width:%.1f%%;background:%s"></i></div><b>%s</b></div>'
                   % (esc(t), cell["n"], 100.0 * p / mx, COUL[t], txt))
    return "".join(out)


def merged_media(R, minn=60):
    """Médias (variantes d'un même nom fusionnées) avec au moins minn titres : [{label, type, n, fam}] triés par nombre de titres."""
    out = {}
    for (t, m), v in R["med"].items():
        lab = v.get("label") or m
        key = (t, C.nm(lab))
        if key in out:
            o = out[key]
            o["n"] += v["n"]
            for f in C.FAMILIES:
                o["fam"][f] = o["fam"].get(f, 0) + v["fam"].get(f, 0)
        else:
            out[key] = {"label": lab, "type": t, "n": v["n"], "fam": {f: v["fam"].get(f, 0) for f in C.FAMILIES}}
    return sorted((o for o in out.values() if o["n"] >= minn), key=lambda o: -o["n"])


def build(R, esc, base):
    G, days = R["glob"], R["days"]
    big = R["big"]
    biggest = max(big, key=lambda d: R["day_n"][d]) if big else None
    legend = '<div class="lg">' + "".join('<span><i style="background:%s"></i>%s</span>' % (COUL[t], esc(t)) for t in C.TYPE_ORDER) + "</div>"
    maj = datetime.fromtimestamp(R["at"]).strftime("%d/%m/%Y")
    # ---------- partie 1 : simple ----------
    g_sent = gap_sentence(G, "Sur toute la période")
    fam_blocks = []
    for f in C.SIMPLE:
        nom, desc, _ = C.FAMILIES[f]
        scale = max([pct(G[t], "fam", f) for t in C.TYPE_ORDER if ok(G.get(t))] + [1]) * 1.15
        fam_blocks.append('<div class="fam"><h3>%s</h3><p class="sub">Titres qui contiennent des mots comme : %s</p>%s</div>' % (esc(nom), esc(desc), bars(G, f, esc, scale)))
    chips, pans = [], []
    for i, d in enumerate(sorted(big, key=lambda d: -R["day_n"][d])):
        on = " on" if i == 0 else ""
        chips.append('<button type="button" class="dchip%s" data-d="%s">%s <small>(%d titres)</small></button>' % (on, d, esc(date_fr(d)), R["day_n"][d]))
        blocks = []
        for f in C.SIMPLE:
            nom, desc, _ = C.FAMILIES[f]
            scale = max([pct(days[d].get(t), "fam", f) for t in C.TYPE_ORDER if ok(days[d].get(t))] + [1]) * 1.15
            blocks.append('<div class="fam"><h3>%s</h3><p class="sub">%s</p>%s</div>' % (esc(nom), esc(desc), bars(days[d], f, esc, scale)))
        pans.append('<div class="pan%s" id="p-%s"><p class="read">%s</p>%s</div>' % (on, d, gap_sentence(days[d], "Le " + date_fr(d)), "".join(blocks)))
    MM = merged_media(R)
    X = R.get("X")
    sx, dx, xcss, xjs = CX.render(X, esc, legend) if X and X.get("pairs") else ("", "", "", "")
    fchips, fpans = [], []
    for i, f in enumerate(C.SIMPLE):
        nom, desc, _ = C.FAMILIES[f]
        L = sorted(MM[:25], key=lambda o: -o["fam"][f] / o["n"])  # les 25 médias les plus couverts ; tous sont dans le face à face et la partie données
        mx = max([100.0 * o["fam"][f] / o["n"] for o in L] + [1]) * 1.1
        rows = "".join('<div class="bar"><span>%s <small style="color:var(--mute)">(%s, %d titres)</small></span><div class="tr"><i style="width:%.1f%%;background:%s"></i></div><b title="± %s point(s), 95 %%">%d sur 100</b></div>'
                       % (esc(o["label"]), esc(o["type"]), o["n"], 100.0 * (100.0 * o["fam"][f] / o["n"]) / mx, COUL[o["type"]], f1(moe(100.0 * o["fam"][f] / o["n"], o["n"])), round(100.0 * o["fam"][f] / o["n"])) for o in L)
        on = " on" if i == 0 else ""
        fchips.append('<button type="button" class="dchip%s" data-f="%s">%s</button>' % (on, f, esc(nom)))
        fpans.append('<div class="fpan%s" id="f-%s"><p class="sub" style="color:var(--mute);font-size:13.5px">Titres qui contiennent des mots comme : %s. Du plus fréquent au moins fréquent.</p>%s</div>' % (on, f, esc(desc), rows))
    vsm = [{"l": o["label"], "t": o["type"], "n": o["n"], "f": {f: o["fam"][f] for f in C.SIMPLE}} for o in sorted(MM, key=lambda o: (o["type"], -o["n"]))]
    d1 = next((o["label"] for o in MM if o["type"] == "Presse nationale"), MM[0]["label"] if MM else "")
    d2 = next((o["label"] for o in MM if o["type"] == "Presse régionale"), d1)
    comparatif = (
        "<h2>Média par média</h2>"
        "<p>Les familles cachent des différences entre journaux d'une même famille. Voici les 25 médias les plus couverts, sur toute la période. Choisis un type de mots :</p>"
        '<div class="dchips" id="fchips">' + "".join(fchips) + "</div>" + legend + "".join(fpans)
        + "<h3>Face à face : deux médias au choix</h3>"
        "<p>Choisis deux médias pour les comparer sur les familles de mots. Un écart est signalé seulement s'il est assez grand pour ne pas venir du hasard.</p>"
        '<div class="vs"><select id="vsa" aria-label="Premier média"></select><span>contre</span><select id="vsb" aria-label="Second média"></select></div>'
        '<div id="vsout"></div><script id="vsdata" type="application/json">'
        + json.dumps({"m": vsm, "fam": {f: C.FAMILIES[f][0] for f in C.SIMPLE}, "col": COUL, "d": [d1, d2]}, ensure_ascii=False, separators=(",", ":")).replace("</", "<" + chr(92) + "/") + "</script>")
    simple = (
        "<h1>Les mots des journaux : qui écrit quoi quand ça manifeste ?</h1>"
        '<p class="lead">Quand des gens manifestent, chaque journal, chaque télé, chaque radio écrit un titre. Ces titres n\'utilisent pas tous les mêmes mots. '
        "Ici, on les compte. On ne dit pas qui a raison : on regarde seulement quels mots reviennent le plus souvent, et chez qui. "
        '<a href="#donnees">Les données complètes sont plus bas.</a></p>'
        '<div class="kpi"><div><b>%s</b>titres lus (sans doublons)</div><div><b>%s</b>rangés par type de média</div><div><b>%d</b>jours très couverts comparés</div><div><b>%s</b>dernière mise à jour</div></div>'
        % (f"{R['total']:,}".replace(",", " "), f"{R['classified']:,}".replace(",", " "), len(big), maj)
        + "<h2>Comment on a compté</h2>"
        '<div class="cards"><div><h4>1. On lit les titres</h4>Chaque jour, le site lit les titres de la presse en ligne qui parlent de manifestations, de blocages ou de grèves.</div>'
        "<div><h4>2. On range les journaux</h4>Cinq familles : presse nationale (Le Monde, Le Figaro…), télés et radios d'info privées (BFMTV, CNews…), service public (franceinfo, France 3, ici), presse régionale (Ouest-France, La Dépêche…) et sites d'infos locales.</div>"
        "<div><h4>3. On fait des listes de mots</h4>Par exemple « désordre » : casseurs, incendies, dégâts. Ou « autorités » : police, préfet, gouvernement. Ou « voix des manifestants » : colère, revendications.</div>"
        "<div><h4>4. On compte</h4>Pour chaque famille de journaux : sur 100 titres, combien contiennent au moins un mot de la liste ?</div></div>"
        '<p class="meta">Compter des mots, ce n\'est pas juger. Si des poubelles ont vraiment brûlé, les journaux ont raison de le dire. Ce qui nous intéresse, c\'est quand les chiffres changent d\'une famille de journaux à l\'autre.</p>'
        + "<h2>Sur toute la période</h2>"
        '<p class="read">' + g_sent + "</p>" + legend + "".join(fam_blocks)
        + "<h2>Le même jour, chez tous les journaux</h2>"
        "<p>Les jours où il se passe beaucoup de choses, tous les journaux écrivent sur les mêmes faits. C'est le meilleur moment pour comparer : si les mots changent, ce n'est pas parce que l'actualité n'était pas la même. Choisis un jour :</p>"
        '<div class="dchips" id="dchips">' + "".join(chips) + "</div>" + legend + "".join(pans)
        + comparatif + sx
        + '<h2>Et à la télé ?</h2><p>Les mêmes listes de mots, appliquées au direct de franceinfo : de quoi parle la télé, et avec quels mots ? '
          '<a href="' + base + '/franceinfo">Voir la page franceinfo</a> (une seule chaîne, mise à jour toutes les trois heures, comptages uniquement).</p>'
        + "<h2>Comment lire ces graphiques</h2>"
        '<div class="cards"><div><h4>Ce que ça montre</h4>Les mots que chaque famille de journaux choisit le plus souvent dans ses titres, et si cela change d\'une famille à l\'autre.</div>'
        "<div><h4>Ce que ça ne montre pas</h4>Ni ce que pense un journal, ni si ses titres sont justes. Un titre peut parler de désordre parce qu'il y en a eu. Et on ne lit que les titres, pas les articles.</div>"
        "<div><h4>Pourquoi les chiffres bougent</h4>Les journaux ne couvrent pas les mêmes villes, et certains reprennent la même dépêche. Quand un écart reste le même jour après jour, ou le même jour chez tous les journaux, il compte davantage.</div></div>"
        '<p class="meta"><a href="#licence">Données réutilisables</a> : libres pour la recherche, sur accord pour un usage commercial. Les écarts trop petits pour être sûrs ne sont pas commentés. Quand une case a moins de %d titres, on ne l\'affiche pas.</p>' % C.MIN_CELL)
    # ---------- partie 2 : données ----------
    th = "".join("<th>%s</th>" % esc(t) for t in C.TYPE_ORDER)
    # 2a sous-catégories globales
    rows = []
    for f, (nom, desc, subs) in C.FAMILIES.items():
        rows.append('<tr class="g"><td colspan="%d">%s</td></tr>' % (len(C.TYPE_ORDER) + 1, esc(nom)))
        rows.append("<tr><td><i>Tous mots de la famille</i></td>" + "".join(
            ('<td title="± %s point(s), 95 %%">%s %%</td>' % (f1(moe(pct(G[t], "fam", f), G[t]["n"])), f1(pct(G[t], "fam", f)))) if ok(G.get(t)) else "<td>–</td>" for t in C.TYPE_ORDER) + "</tr>")
        for k in subs:
            rows.append("<tr><td>%s</td>" % esc(k) + "".join(
                ('<td title="± %s point(s), 95 %%">%s %%</td>' % (f1(moe(pct(G[t], "sub", (f, k)), G[t]["n"])), f1(pct(G[t], "sub", (f, k))))) if ok(G.get(t)) else "<td>–</td>" for t in C.TYPE_ORDER) + "</tr>")
    t_glob = '<div class="tw"><table><thead><tr><th>Part des titres contenant…</th>%s</tr></thead><tbody>%s<tr><td><b>Titres analysés</b></td>%s</tr></tbody></table></div>' % (
        th, "".join(rows), "".join("<td><b>%s</b></td>" % f"{G[t]['n']:,}".replace(",", " ") for t in C.TYPE_ORDER))
    # 2b par jour
    day_tabs = []
    for d in sorted(big, key=lambda d: -R["day_n"][d]):
        rr = []
        for f, (nom, _, _) in C.FAMILIES.items():
            rr.append("<tr><td>%s</td>" % esc(nom) + "".join(
                ('<td title="± %s point(s), 95 %%">%s %%</td>' % (f1(moe(pct(days[d][t], "fam", f), days[d][t]["n"])), f1(pct(days[d][t], "fam", f)))) if ok(days[d].get(t)) else "<td>–</td>" for t in C.TYPE_ORDER) + "</tr>")
        rr.append("<tr><td><b>Titres analysés</b></td>" + "".join("<td><b>%d</b></td>" % days[d][t]["n"] if days[d].get(t) else "<td>0</td>" for t in C.TYPE_ORDER) + "</tr>")
        day_tabs.append("<h3>%s <small>(%d titres uniques dans la base ce jour-là)</small></h3>" % (esc(date_fr(d)), R["day_n"][d])
                        + '<div class="tw"><table><thead><tr><th>Famille de mots</th>%s</tr></thead><tbody>%s</tbody></table></div>' % (th, "".join(rr)))
    # 2c par média
    med_tabs = []
    hf = "".join("<th>%s</th>" % esc(C.FAMILIES[f][0]) for f in C.FAMILIES)
    for t in C.TYPE_ORDER:
        L = sorted([(v.get("label") or m, v) for (tt, m), v in R["med"].items() if tt == t and v["n"] >= 60], key=lambda x: -x[1]["n"])
        merged = {}
        for lab, v in L:  # fusionne les variantes d'un même nom (ex. Actu.fr / actu.fr)
            key = C.nm(lab)
            if key in merged:
                m = merged[key]
                m["n"] += v["n"]
                for f in C.FAMILIES:
                    m["fam"][f] = m["fam"].get(f, 0) + v["fam"].get(f, 0)
            else:
                merged[key] = {"label": lab, "n": v["n"], "fam": dict(v["fam"])}
        if not merged:
            continue
        rr = []
        for m in sorted(merged.values(), key=lambda x: -x["n"]):
            rr.append("<tr><td>%s</td><td>%d</td>" % (esc(m["label"]), m["n"]) + "".join("<td>%s %%</td>" % f1(100.0 * m["fam"].get(f, 0) / m["n"]) for f in C.FAMILIES) + "</tr>")
        med_tabs.append('<h3>%s</h3><div class="tw"><table><thead><tr><th>Média</th><th>Titres</th>%s</tr></thead><tbody>%s</tbody></table></div>' % (esc(t), hf, "".join(rr)))
    # 2d méthode
    lex = []
    for f, (nom, desc, subs) in C.FAMILIES.items():
        for k, rx in subs.items():
            lex.append("<tr><td>%s</td><td>%s</td><td><code>%s</code></td></tr>" % (esc(nom), esc(k), esc(rx)))
    t_lex = '<div class="tw"><table><thead><tr><th>Famille</th><th>Sous-catégorie</th><th>Expression cherchée (sans tenir compte des majuscules)</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(lex)
    names = {}
    for (tt, m), v in R["med"].items():
        names.setdefault(tt, {}).setdefault(C.nm(v.get("label") or m), [v.get("label") or m, 0])[1] += v["n"]
    t_src = '<div class="tw"><table><thead><tr><th>Type</th><th>Sources rangées dans ce type (titres)</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(
        "<tr><td>%s</td><td style=\"white-space:normal;text-align:left\">%s</td></tr>" % (esc(t), esc(" · ".join("%s (%d)" % (n, c) for n, c in sorted(names.get(t, {}).values(), key=lambda x: -x[1])))) for t in C.TYPE_ORDER)
    unk = ", ".join("%s (%d)" % (esc(s), n) for s, n in R["unk_top"])
    donnees = (
        '<h1 id="donnees" style="margin-top:50px">Les données complètes et la méthode</h1>'
        '<p class="lead">Tout ce qui précède vient des tableaux ci-dessous. Les chiffres sont des parts de titres ; l\'infobulle de chaque case donne la marge d\'erreur à 95 %. '
        'Les données se téléchargent en <a href="' + base + '/cadrage.csv">CSV</a>. <strong>Ce CSV est exploitable gratuitement par les chercheurs</strong> (avec mention de la source ; <a href="#licence">licence et conditions</a>).</p>'
        '<h2 id="licence">Licence et réutilisation</h2>'
        '<div class="cards"><div><h4>Libre et gratuit</h4>La recherche et l\'enseignement, ainsi que les associations, les syndicats et les journalistes pour un article ou une infographie : tableaux, listes de mots et CSV sont réutilisables avec la mention de la source.</div>'
        "<div><h4>Sur accord préalable</h4>Tout usage commercial : entreprises, cabinets de conseil, produits ou services payants. <a href=\"mailto:ADRESSE_DE_CONTACT\">Demandez l'accord de l'éditeur.</a></div>"
        '<div><h4>Conditions</h4>Licence <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/deed.fr" rel="license noopener">CC BY-NC-SA 4.0</a> (source citée, pas d\'usage commercial, partage dans les mêmes conditions). '
        "Mention : « Les mots des journaux, ça brûle, oraclemarin.fr/cabrule/cadrage ». Les titres de presse analysés restent la propriété de leurs éditeurs ; seuls les comptages sont publiés.</div></div>"
        "<h2>Méthode</h2>"
        "<ul><li><strong>Corpus.</strong> Les titres déjà collectés par ça brûle (flux RSS et résultats d'actualités de la presse en ligne) qui parlent de manifestations, blocages, grèves ou rassemblements, après le tri qui écarte le hors-sujet et les rétrospectives. "
        "Un titre repris à l'identique par plusieurs médias n'est compté qu'une fois. Un compte rendu tardif est daté du jour de l'événement. Période : depuis le 30 septembre 2026.</li>"
        "<li><strong>Types de médias.</strong> Rangement fait à la main, source par source (tableau plus bas). Les sources qui ne sont pas de la presse (réseaux sociaux, portails qui republient, radios isolées) ne sont pas comptées : %d titres sur %d.</li>"
        "<li><strong>Comptage.</strong> Un titre est compté dans une famille s'il contient au moins un mot de sa liste (un titre peut compter dans plusieurs familles). La part est le nombre de titres contenant au moins un de ces mots, divisé par le nombre de titres du type de média.</li>"
        "<li><strong>Incertitude.</strong> Marge à 95 %% = 1,96 × √(p(1−p)/n). Les cases de moins de %d titres ne sont pas publiées. Les phrases automatiques de la partie simple ne citent un écart que s'il dépasse 2,5 fois son incertitude.</li>"
        "<li><strong>Jours comparés.</strong> Les %d jours les plus couverts (au moins 150 titres), hors le jour en cours. La liste change toute seule quand un nouveau jour dépasse les autres.</li></ul>"
        "<h2>Limites</h2>"
        "<ul><li>On compte des mots dans des titres, pas des intentions : « incendie » peut décrire un fait réel, « police » peut figurer dans « la police a interpellé » comme dans « violences policières ».</li>"
        "<li>Les médias ne couvrent pas les mêmes lieux : la presse régionale parle surtout du local, la nationale des grandes villes. La comparaison le même jour réduit cet effet sans le supprimer.</li>"
        "<li>Les dépêches d'agence sont reprises par de nombreux titres : les doublons identiques sont retirés, pas les reformulations proches.</li>"
        "<li>Certains titres de la base viennent de moteurs d'actualités qui les reformulent. Le nom du média y est toutefois celui de la source d'origine.</li>"
        "<li>La liste de mots est un choix éditorial, publié en entier ci-dessous. Un autre choix donnerait d'autres chiffres, c'est pourquoi tout est ouvert à la critique : <a href=\"mailto:ADRESSE_DE_CONTACT\">écrivez-nous</a> pour proposer un mot.</li>"
        "<li>Aucune étiquette politique n'est attribuée aux médias.</li></ul>"
        % (R["unclassified"], R["total"], C.MIN_CELL, len(big))
        + "<p class=\"meta\">Sources non rangées les plus fréquentes : " + unk + ".</p>"
        + "<h2>Sur toute la période</h2>" + t_glob
        + "<h2>Jour par jour</h2>" + "".join(day_tabs)
        + "<h2>Média par média (au moins 60 titres)</h2>" + "".join(med_tabs)
        + dx + "<h2>Listes de mots</h2>" + t_lex
        + "<h2>Sources rangées par type</h2>" + t_src)
    js = r"""(function(){var cs=document.querySelectorAll('#dchips .dchip');cs.forEach(function(b){b.onclick=function(){cs.forEach(function(x){x.classList.toggle('on',x===b)});
document.querySelectorAll('.cd .pan').forEach(function(p){p.classList.toggle('on',p.id==='p-'+b.getAttribute('data-d'))})}})})();"""
    js += r"""(function(){var fc=document.querySelectorAll('#fchips .dchip');fc.forEach(function(b){b.onclick=function(){fc.forEach(function(x){x.classList.toggle('on',x===b)});
document.querySelectorAll('.cd .fpan').forEach(function(p){p.classList.toggle('on',p.id==='f-'+b.getAttribute('data-f'))})}});
var el=document.getElementById('vsdata');if(!el)return;var D=JSON.parse(el.textContent),A=document.getElementById('vsa'),B=document.getElementById('vsb'),O=document.getElementById('vsout');
function opt(s,sel){D.m.forEach(function(m,i){var o=document.createElement('option');o.value=i;o.textContent=m.l+' ('+m.t+')';if(m.l===sel)o.selected=true;s.appendChild(o)})}
opt(A,D.d[0]);opt(B,D.d[1]);
function f1(x){return (Math.round(x*10)/10).toString().replace('.',',')}
function draw(){var a=D.m[A.value],b=D.m[B.value],h='';
 Object.keys(D.fam).forEach(function(f){var pa=100*a.f[f]/a.n,pb=100*b.f[f]/b.n,mx=Math.max(pa,pb,1)*1.15,se=Math.sqrt(pa/100*(1-pa/100)/a.n+pb/100*(1-pb/100)/b.n)*100,z=se?Math.abs(pa-pb)/se:0;
  var note=z>=2.5?'<div class="vsl"><b>Écart net :</b> '+(pa>pb?a.l:b.l)+' en emploie plus ('+Math.round(Math.max(pa,pb))+' contre '+Math.round(Math.min(pa,pb))+' sur 100).</div>':'<div class="vsl">Pas d&#39;écart net.</div>';
  h+='<div class="fam"><h3>'+D.fam[f]+'</h3>'
   +'<div class="bar"><span>'+a.l+' <small style="color:var(--mute)">('+a.n+' titres)</small></span><div class="tr"><i style="width:'+(100*pa/mx)+'%;background:'+D.col[a.t]+'"></i></div><b>'+Math.round(pa)+' sur 100</b></div>'
   +'<div class="bar"><span>'+b.l+' <small style="color:var(--mute)">('+b.n+' titres)</small></span><div class="tr"><i style="width:'+(100*pb/mx)+'%;background:'+D.col[b.t]+'"></i></div><b>'+Math.round(pb)+' sur 100</b></div>'+note+'</div>'});
 O.innerHTML=h}
A.onchange=B.onchange=draw;draw()})();"""
    return CSS + xcss, '<div class="cd">' + simple + donnees + "</div><script>" + js + xjs + "</script>"


def csv_text(R):
    out = ["portee,jour,type,media,titres,nature,cle,titres_avec,part_pct"]
    q = lambda s: '"%s"' % str(s).replace('"', '""')

    def emit(scope, day, t, med, cell):
        if not cell or cell["n"] < 1:
            return
        for f in C.FAMILIES:
            out.append(",".join([scope, day, q(t), q(med), str(cell["n"]), "famille", f, str(cell["fam"].get(f, 0)), "%.2f" % (100.0 * cell["fam"].get(f, 0) / cell["n"])]))
        for (f, k), v in sorted(cell["sub"].items()):
            out.append(",".join([scope, day, q(t), q(med), str(cell["n"]), "sous-categorie", q(f + ": " + k), str(v), "%.2f" % (100.0 * v / cell["n"])]))

    for t, cell in R["glob"].items():
        emit("global", "", t, "", cell)
    for d, byt in R["days"].items():
        for t, cell in byt.items():
            emit("jour", d, t, "", cell)
    for (t, m), cell in R["med"].items():
        if cell["n"] >= 60:
            emit("media", "", t, cell.get("label") or m, cell)
    if R.get("X") and R["X"].get("pairs"):
        out += CX.csv_rows(R["X"], q)
    return "\n".join(out) + "\n"
