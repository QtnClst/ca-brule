"""Rendu des analyses croisées de cadrage2.py : même événement, thème, lieu. render(X, esc, legend) -> (simple, detail, css, js)."""
import cadrage as C
import cadrage2 as C2

THEME_LAB = {"lyceens": "Mobilisations lycéennes", "violences": "Violences", "travail": "Travail", "politique": "Politique", "transports": "Transports", "autre": "Autres sujets"}
SHORT = {"Presse nationale": "Nationale", "TV et radio d'info privées": "TV/radio privées", "Service public": "Service public", "Presse régionale": "Régionale", "Sites d'infos locales": "Sites locaux"}
HEADLINE = [("Presse nationale", "Presse régionale"), ("TV et radio d'info privées", "Presse régionale"), ("Service public", "Presse régionale")]
PLACE_FAM = ["desordre", "autorite", "voix", "victimes"]

CSS = """
.cd .dl{margin:8px 0}.cd .dl .r{display:grid;grid-template-columns:170px 1fr;gap:10px;align-items:center;margin:5px 0;font-size:13.5px;color:#c9ced6}
.cd .dl .bb{display:flex;flex-direction:column;gap:3px}.cd .dl .ln{display:grid;grid-template-columns:1fr 150px;gap:8px;align-items:center}
.cd .dl .tr{height:14px;background:#10131a;border:1px solid var(--line);border-radius:5px;overflow:hidden}.cd .dl .tr i{display:block;height:100%}
.cd .dl2{margin:10px 0 4px}.cd .r2{display:grid;grid-template-columns:170px 1fr 200px;gap:10px;align-items:center;margin:6px 0;font-size:13.5px;color:#c9ced6}
.cd .r2 .ax{position:relative;height:18px;background:#10131a;border:1px solid var(--line);border-radius:5px}
.cd .r2 .ax i.bar{position:absolute;top:2px;bottom:2px;border-radius:3px}
.cd .r2 .ax i.bar.flou{opacity:.55;background-image:repeating-linear-gradient(45deg,rgba(0,0,0,.35) 0 4px,transparent 4px 8px)!important}
.cd .r2 .ax u.z{position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:#8b929c;text-decoration:none}
.cd .r2 .ax b.tk{position:absolute;top:-3px;bottom:-3px;width:3px;margin-left:-1px;background:#cbd0d8;border-radius:2px;opacity:.9}
.cd .r2 .vv{font-size:12.5px;color:#fff}.cd .r2 .vv small{color:var(--mute)}
.cd .r2.axe{margin-bottom:2px}.cd .r2 .axl{display:flex;justify-content:space-between;font-size:11.5px;color:var(--mute)}
.cd .r2 .axl .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 6px;vertical-align:-1px}
.cd .r2 .axl span{color:#c9ced6}
.cd .fam p.read{margin:6px 0 10px;font-size:14px;color:#e8eaed}
@media(max-width:700px){.cd .r2{grid-template-columns:1fr}.cd .r2.axe .lb,.cd .r2.axe .vv{display:none}}
.cd .dl .v{font-size:12.5px;color:#fff}.cd .dl .v small{color:var(--mute)}.cd .dl .lab{font-size:11.5px;color:var(--mute)}
.cd .gpan{display:none}.cd .gpan.on{display:block}
.cd td.net{color:#fff;font-weight:700}.cd td.nul{color:var(--mute)}
@media(max-width:700px){.cd .dl .r{grid-template-columns:1fr}.cd .dl .ln{grid-template-columns:1fr 120px}}
"""

JS = r"""(function(){var bs=document.querySelectorAll('.cd .dchip[data-g]');bs.forEach(function(b){b.onclick=function(){var g=b.getAttribute('data-g'),k=b.getAttribute('data-k');
document.querySelectorAll('.cd .dchip[data-g="'+g+'"]').forEach(function(x){x.classList.toggle('on',x===b)});
document.querySelectorAll('.cd .gpan[data-g="'+g+'"]').forEach(function(p){p.classList.toggle('on',p.getAttribute('data-k')===k)})}})})();"""


def f1(x):
    return ("%.1f" % x).replace(".", ",")


def sg(x):
    return ("+" if x >= 0 else "−") + f1(abs(x))


def pct(cell, f):
    return 100.0 * cell["fam"].get(f, 0) / cell["n"] if cell and cell["n"] else 0.0


def net(m, se):
    return abs(m) > 1.96 * se


def dl_row(label, naive, m, se, mx):
    """Deux barres : écart brut (gris) et écart sur le même événement (orange si positif, bleu si négatif), en points de pourcentage."""
    def bar(v, col):
        return '<div class="tr"><i style="width:%.1f%%;background:%s"></i></div>' % (100.0 * min(abs(v), mx) / mx, col)

    col = "#ff5a1f" if m >= 0 else "#38bdf8"
    verdict = "écart net" if net(m, se) else "écart pas sûr"
    nv = ('<div class="ln">' + bar(naive, "#6b7480") + '<span class="v">%s pt <small>sur tous les titres</small></span></div>' % sg(naive)) if naive is not None else ""
    return ('<div class="r"><span>%s</span><div class="bb">%s<div class="ln">%s<span class="v"><b>%s pt</b> <small>même événement · %s</small></span></div></div></div>'
            % (label, nv, bar(m, col), sg(m), verdict))


def dl_row2(label, naive, m, se, mx):
    """Une barre centrée sur zéro : écart à événement égal (vers la droite : le premier média en emploie plus ; vers la gauche : la presse régionale en emploie plus).
    Repère gris : écart brut (sans tenir compte des événements). Barre hachurée : écart pas sûr."""
    sure = net(m, se)
    half = 50.0 * min(abs(m), mx) / mx
    left = 50.0 if m >= 0 else 50.0 - half
    col = "#ff5a1f" if m >= 0 else "#38bdf8"
    cls = "bar" + ("" if sure else " flou")
    tick = ""
    if naive is not None:
        tp = 50.0 + 50.0 * max(-mx, min(mx, naive)) / mx
        tick = '<b class="tk" style="left:%.1f%%" title="Écart sur l\'ensemble des titres : %s pt"></b>' % (tp, sg(naive))
    verdict = "écart net" if sure else "écart trop petit pour être sûr"
    return ('<div class="r2"><span class="lb">%s</span><div class="ax">%s<i class="%s" style="left:%.1f%%;width:%.1f%%;background:%s"></i><u class="z"></u></div>'
            '<span class="vv"><b>%s pt</b> <small>%s</small></span></div>' % (label, tick, cls, left, half, col, sg(m), verdict))


def lecture(a, b, r):
    """Phrase de lecture d'une carte : sujets où l'écart est net à événement égal, dans les deux sens."""
    SUJ = {"desordre": "le désordre", "autorite": "les autorités", "voix": "la voix des manifestants", "victimes": "les manifestants blessés", "parole": "la parole rapportée", "pratique": "les informations pratiques"}
    plus, moins = [], []
    for f in C.SIMPLE:
        m, se = r["fam"][f]
        if net(m, se):
            (plus if m > 0 else moins).append("%s (%s pt)" % (SUJ.get(f, f), sg(m)))
    pl = a.startswith("TV") or a.startswith("Sites")
    nom = {"Presse nationale": "la presse nationale", "TV et radio d'info privées": "les télés et radios d'info privées", "Service public": "le service public"}.get(a, a)
    if not plus and not moins:
        return "À événement égal, aucun écart n'est assez net pour être sûr entre %s et la presse régionale." % nom
    verbe = "ont" if pl else "a"
    t = "À événement égal, %s %s " % (nom, verbe)
    parts = []
    if plus: parts.append("plus de titres avec des mots sur " + ", ".join(plus))
    if moins: parts.append("moins sur " + ", ".join(moins))
    return t + (", et ".join(parts)) + ", que la presse régionale. Pour les autres sujets, l'écart est trop petit pour être sûr."


def _headline0(pairs):
    """Phrase automatique : plus grand écart net, à événement égal, parmi les comparaisons avec la presse régionale."""
    best = None
    for (a, b), r in pairs.items():
        if (a, b) not in HEADLINE:
            continue
        for f in C.SIMPLE:
            m, se = r["fam"][f]
            if net(m, se) and (best is None or abs(m) > abs(best[0])):
                best = (m, f, a, b, r, se)
    if not best:
        return "À événement égal, aucun écart net n'apparaît entre ces types de médias sur ces mots."
    m, f, a, b, r, se = best
    brut = r["naive"][f]
    return ("À événement égal, l'écart le plus net est celui-ci : %s emploie des %s dans <b>%s points de titres en plus</b> que %s (marge ±%s). "
            "Sur l'ensemble des titres, cet écart paraissait de %s points : une partie venait donc de ce que chacun couvre."
            % ({"Presse nationale": "la presse nationale", "TV et radio d'info privées": "les télés et radios d'info privées", "Service public": "le service public"}[a],
               {"desordre": "mots de désordre", "autorite": "mots d'autorité", "voix": "mots de la voix des manifestants", "victimes": "mots sur les manifestants blessés", "parole": "mots de parole rapportée", "pratique": "mots d'informations pratiques"}[f],
               f1(abs(m)), "la presse régionale", f1(1.96 * se), f1(abs(brut)) if brut is not None else "?"))


def headline(pairs):
    t = _headline0(pairs)
    return t.replace("les télés et radios d'info privées emploie", "les télés et radios d'info privées emploient")


def render(X, esc, legend):
    pairs = X["pairs"]
    # ---------- simple : même événement ----------
    cards = []
    mx = max([abs(v) for r in pairs.values() for f in C.SIMPLE for v in (r["fam"][f][0], r["naive"][f] or 0)] + [5.0]) * 1.05
    for a, b in HEADLINE:
        r = pairs.get((a, b))
        if not r:
            continue
        rows = "".join(dl_row2(esc(C.FAMILIES[f][0]), r["naive"][f], r["fam"][f][0], r["fam"][f][1], mx) for f in C.SIMPLE)
        axe = '<div class="r2 axe"><span class="lb"></span><div class="axl"><span><i class="sw" style="background:#38bdf8"></i>← plus chez la presse régionale</span><span>plus chez %s →<i class="sw" style="background:#ff5a1f"></i></span></div><span class="vv"></span></div>' % esc(a.lower() if not a.startswith("TV") else "TV et radio d'info privées")
        cards.append('<div class="fam"><h3>%s comparée à la presse régionale</h3><p class="sub">%d événements couverts par les deux (%d et %d titres)</p><p class="read">%s</p><div class="dl2">%s%s</div></div>'
                     % (esc(a), r["events"], r["titles_a"], r["titles_b"], lecture(a, "Presse régionale", r), axe, rows))
    simple = (
        "<h2>Et si on compare sur le même événement ?</h2>"
        "<p>Jusqu'ici, un média pouvait sembler différent simplement parce qu'il parle d'autres villes. Ici, on ne garde que les événements (même ville, même jour) couverts par <b>les deux</b> types de médias, "
        "et on mesure l'écart entre eux pour chaque événement. Un écart de « +6 points » veut dire : 6 titres de plus sur 100 contiennent ces mots que chez l'autre type de média.</p>"
        '<p class="read">' + headline(pairs) + "</p>" + "".join(cards)
        + '<p class="meta">Comment lire : la barre part du milieu (zéro). Vers la droite (orange), le premier média emploie plus ces mots que la presse régionale ; vers la gauche (bleu), moins. '
        "« pt » = points de pourcentage : +5 pt veut dire 5 titres de plus sur 100. Le petit trait gris montre l\'écart quand on compare tous les titres sans tenir compte des événements ; s\'il est plus loin que la barre, une partie de l\'écart venait de ce que chaque média couvre. "
        "Barre hachurée : l\'écart est dans la marge d\'erreur (95 %), donc pas sûr.</p>")
    # ---------- simple : thèmes ----------
    chips, pans = [], []
    th = X["themes"]
    for i, f in enumerate(C.SIMPLE):
        on = " on" if i == 0 else ""
        chips.append('<button type="button" class="dchip%s" data-g="th" data-k="%s">%s</button>' % (on, f, esc(C.FAMILIES[f][0])))
        head = "<tr><th>Sujet</th>" + "".join("<th>%s</th>" % esc(SHORT[t]) for t in C.TYPE_ORDER) + "</tr>"
        body = ""
        for t in th:
            cells = X["theme_cells"][t]
            body += "<tr><td>%s <small>(%d titres)</small></td>" % (esc(THEME_LAB.get(t, t)), X["theme_tot"][t]) + "".join(
                ("<td>%d sur 100</td>" % round(pct(cells[ty], f))) if cells.get(ty) and cells[ty]["n"] >= C.MIN_CELL else '<td class="nul">–</td>' for ty in C.TYPE_ORDER) + "</tr>"
        pans.append('<div class="gpan%s" data-g="th" data-k="%s"><div class="tw"><table><thead>%s</thead><tbody>%s</tbody></table></div></div>' % (on, f, head, body))
    simple += (
        "<h2>Est-ce que ça change selon le sujet ?</h2>"
        "<p>Les titres ne parlent pas tous de la même chose : lycéens, travail, politique… Voici, pour un type de mots, la part de titres de chaque famille de médias, <b>sujet par sujet</b>. "
        "Quand l'écart reste le même d'un sujet à l'autre, il tient au média ; quand il disparaît, il tenait au sujet. Un tiret veut dire : moins de %d titres, trop peu pour conclure.</p>" % C.MIN_CELL
        + '<div class="dchips">' + "".join(chips) + "</div>" + "".join(pans)
        + '<p class="meta">Le thème « Violences » regroupe justement des titres sur des violences : y trouver des mots de désordre est attendu.</p>')
    # ---------- simple : lieu ----------
    P = X.get("place")
    if P:
        chips2, pans2 = [], []
        for i, (scope, lab) in enumerate((("tous", "Tous les sujets"), ("lyceens", "Mobilisations lycéennes seulement"))):
            on = " on" if i == 0 else ""
            chips2.append('<button type="button" class="dchip%s" data-g="pl" data-k="%s">%s</button>' % (on, scope, esc(lab)))
            blocks = []
            for f in PLACE_FAM:
                vals = [pct(P[scope][b]["ALL"], f) for b, _ in C2.BUCKETS if P[scope][b]["ALL"]["n"] >= C.MIN_CELL]
                m = max(vals + [1.0]) * 1.15
                rows = "".join(
                    ('<div class="bar"><span>%s <small style="color:var(--mute)">(%d titres, %d communes)</small></span><div class="tr"><i style="width:%.1f%%;background:#ff5a1f"></i></div><b>%s sur 100</b></div>'
                     % (esc(bl), P[scope][b]["ALL"]["n"], P[scope][b]["ALL"]["communes"], 100.0 * pct(P[scope][b]["ALL"], f) / m, f1(pct(P[scope][b]["ALL"], f))))
                    if P[scope][b]["ALL"]["n"] >= C.MIN_CELL else '<div class="bar nd"><span>%s</span><div class="tr"></div><b>trop peu de titres</b></div>' % esc(bl)
                    for b, bl in C2.BUCKETS)
                blocks.append('<div class="fam"><h3>%s</h3>%s</div>' % (esc(C.FAMILIES[f][0]), rows))
            pans2.append('<div class="gpan%s" data-g="pl" data-k="%s">%s</div>' % (on, scope, "".join(blocks)))
        conc = []
        for b, bl in C2.BUCKETS:
            c_ = P["tous"][b]["ALL"]
            if c_["n"] >= C.MIN_CELL and c_["top"][1] >= 25:
                conc.append("%s : %s concentre %d %% des titres" % (bl.lower(), c_["top"][0], round(c_["top"][1])))
        simple += (
            "<h2>Est-ce que ça change selon l'endroit ?</h2>"
            "<p>On regroupe les communes de plus de 20 000 habitants selon la part de leurs habitants qui vivent en quartier prioritaire de la politique de la ville (QPV), puis on regarde les mots des titres qui parlent de ces communes. "
            "Cela ne dit rien du niveau de violence réel des quartiers : cela dit seulement <b>comment la presse en parle</b>.</p>"
            '<div class="dchips">' + "".join(chips2) + "</div>" + "".join(pans2)
            + '<p class="meta">Prudence : ces groupes pèsent peu de communes et quelques affaires très médiatisées comptent beaucoup. ' + (" ; ".join(conc) + ". " if conc else "")
            + "Les communes sans QPV sont peu nombreuses parmi les grandes villes touchées : leurs chiffres sont fragiles.</p>")
    # ---------- détail ----------
    det = ["<h2>Même événement : tous les écarts</h2>"
           "<p>Événement = une commune et un jour. Pour chaque paire de types de médias, on garde les événements où chacun a au moins %d titres (au moins %d événements pour publier). "
           "L'écart est la moyenne, sur ces événements, de (part du premier type − part du second), pondérée par n₁n₂/(n₁+n₂). Marge à 95 %% = 1,96 × erreur type calculée par événement (événements considérés indépendants). "
           "« Brut » = écart sur l'ensemble des titres des deux types.</p>" % (C2.MIN_EV_TITLES, C2.MIN_EVENTS)]
    head = "<tr><th>Paire de types</th><th>Événements</th>" + "".join("<th>%s</th>" % esc(C.FAMILIES[f][0]) for f in C.FAMILIES) + "</tr>"
    rows = ""
    for (a, b), r in pairs.items():
        cells = ""
        for f in C.FAMILIES:
            m, se = r["fam"][f]
            br = r["naive"][f]
            cells += '<td class="%s" title="brut : %s pt">%s ± %s</td>' % ("net" if net(m, se) else "nul", sg(br) if br is not None else "?", sg(m), f1(1.96 * se))
        rows += "<tr><td>%s contre %s</td><td>%d</td>%s</tr>" % (esc(a), esc(b), r["events"], cells)
    det.append('<div class="tw"><table><thead>%s</thead><tbody>%s</tbody></table></div><p class="meta">Points de pourcentage ; en gras : écart net. L\'infobulle donne l\'écart brut.</p>' % (head, rows))
    det.append("<h2>Par sujet : toutes les familles</h2>")
    for t in th:
        cells = X["theme_cells"][t]
        head = "<tr><th>Famille de mots</th>" + "".join("<th>%s</th>" % esc(SHORT[ty]) for ty in C.TYPE_ORDER) + "</tr>"
        body = ""
        for f in C.FAMILIES:
            body += "<tr><td>%s</td>" % esc(C.FAMILIES[f][0]) + "".join(
                ("<td>%s %%</td>" % f1(pct(cells[ty], f))) if cells.get(ty) and cells[ty]["n"] >= C.MIN_CELL else "<td>–</td>" for ty in C.TYPE_ORDER) + "</tr>"
        body += "<tr><td><b>Titres</b></td>" + "".join("<td><b>%d</b></td>" % (cells[ty]["n"] if cells.get(ty) else 0) for ty in C.TYPE_ORDER) + "</tr>"
        det.append("<h3>%s <small>(%d titres)</small></h3>" % (esc(THEME_LAB.get(t, t)), X["theme_tot"][t]) + '<div class="tw"><table><thead>%s</thead><tbody>%s</tbody></table></div>' % (head, body))
    if P:
        det.append("<h2>Par lieu : toutes les familles</h2>"
                   "<p>Communes d'au moins %d habitants ; classées avec la part de population en QPV (base de la politique de la ville, population Insee). Un titre est rattaché à une commune quand elle y est reconnue. "
                   "Les communes absentes de la liste des QPV sont classées « aucun ».</p>" % C2.MIN_POP)
        for scope, lab in (("tous", "Tous les sujets"), ("lyceens", "Mobilisations lycéennes seulement")):
            head = "<tr><th>Lieu</th><th>Titres</th><th>Communes</th><th>Commune la plus citée</th>" + "".join("<th>%s</th>" % esc(C.FAMILIES[f][0]) for f in C.FAMILIES) + "</tr>"
            body = ""
            for b, bl in C2.BUCKETS:
                c_ = P[scope][b]["ALL"]
                body += "<tr><td>%s</td><td>%d</td><td>%d</td><td>%s (%s %%)</td>" % (esc(bl), c_["n"], c_["communes"], esc(c_["top"][0]), f1(c_["top"][1])) + "".join(
                    ("<td>%s %%</td>" % f1(pct(c_, f))) if c_["n"] >= C.MIN_CELL else "<td>–</td>" for f in C.FAMILIES) + "</tr>"
            det.append("<h3>%s</h3>" % esc(lab) + '<div class="tw"><table><thead>%s</thead><tbody>%s</tbody></table></div>' % (head, body))
    return simple, "".join(det), CSS, JS


def csv_rows(X, q):
    out = []
    for (a, b), r in X["pairs"].items():
        for f in C.FAMILIES:
            m, se = r["fam"][f]
            out.append(",".join(["meme_evenement", "", q(a + " | " + b), "", str(r["events"]), "ecart_points", f, "%.2f" % m, "%.2f" % (1.96 * se)]))
    for t in X["themes"]:
        for ty, cell in X["theme_cells"][t].items():
            for f in C.FAMILIES:
                if cell["n"]:
                    out.append(",".join(["theme", "", q(ty), q(t), str(cell["n"]), "famille", f, str(cell["fam"].get(f, 0)), "%.2f" % (100.0 * cell["fam"].get(f, 0) / cell["n"])]))
    for scope, g in (X.get("place") or {}).items():
        for b, _ in C2.BUCKETS:
            for ty, cell in g[b].items():
                if isinstance(cell, dict) and "n" in cell and cell["n"] and ty != "communes":
                    for f in C.FAMILIES:
                        out.append(",".join(["lieu_" + scope, "", q(ty), q(b), str(cell["n"]), "famille", f, str(cell["fam"].get(f, 0)), "%.2f" % (100.0 * cell["fam"].get(f, 0) / cell["n"])]))
    return out
