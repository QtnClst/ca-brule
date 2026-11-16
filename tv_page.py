"""Page /franceinfo : de quoi parle le direct de franceinfo et avec quels mots, à partir de comptages (jamais de texte).

build(D, press, esc, base) -> (css, body). D = contenu de tv_data.json ; press = {jour: nombre de titres de presse (ça brûle)}."""
import html
import math
from collections import defaultdict

import cadrage as C

MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
TCOL = {"conflit": "#ef4444", "politique": "#38bdf8", "mobilisation": "#ff5a1f", "international": "#9b6bff", "economie": "#ffb020", "justice": "#f43f5e",
        "environnement": "#17c3a2", "culture": "#e879f9", "sante": "#84cc16", "sport": "#60a5fa", "transports": "#14b8a6", "autre": "#6b7480"}
FCOL = ["#ff5a1f", "#38bdf8", "#9b6bff", "#17c3a2", "#ffb020", "#f43f5e", "#84cc16", "#e879f9", "#14b8a6", "#a3a3a3"]

CSS = """
.tv h2{margin:34px 0 8px}.tv .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin:16px 0}
.tv .row{display:flex;gap:26px;flex-wrap:wrap;align-items:center}.tv .row ul{list-style:none;padding:0;margin:0;flex:1 1 250px}.tv .row li{margin:5px 0;font-size:14.5px}
.tv .row li i,.tv .lg i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:8px}.tv small{color:var(--mute)}
.tv .kpi{display:flex;flex-wrap:wrap;gap:10px 22px;margin:12px 0;color:#c9ced6;font-size:14px}.tv .kpi b{color:#fff;font-size:20px;display:block}
.tv .st{display:grid;grid-template-columns:120px 1fr 70px;gap:10px;align-items:center;margin:6px 0;font-size:13.5px;color:#c9ced6}
.tv .st .bar{display:flex;height:22px;border-radius:7px;overflow:hidden;background:#10131a;border:1px solid var(--line)}.tv .st .bar i{display:block;height:100%}
.tv .lg{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:#c9ced6;margin:8px 0}
.tv .tw{overflow-x:auto;border:1px solid var(--line);border-radius:12px;margin:10px 0}.tv table{border-collapse:collapse;width:100%;font-size:13px;min-width:520px}
.tv th,.tv td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}.tv th:first-child,.tv td:first-child{text-align:left}.tv thead th{background:#10131a;color:#c9ced6}
@media(max-width:700px){.tv .st{grid-template-columns:90px 1fr 54px}}
"""


def date_fr(d):
    try:
        y, m, j = d.split("-")
        return "%d %s" % (int(j), MOIS[int(m) - 1])
    except Exception:
        return d


def pie(items, size=250):
    tot = float(sum(v for _, v, _ in items)) or 1.0
    cx = cy = size / 2
    r, ri = size / 2 - 6, size / 2 - 50
    a0, parts, leg = -math.pi / 2, [], []
    for lab, v, col in items:
        if v <= 0:
            continue
        frac = v / tot
        a1 = a0 + 2 * math.pi * frac
        if frac > 0.9999:
            parts.append('<circle cx="%.1f" cy="%.1f" r="%.1f" fill="none" stroke="%s" stroke-width="%.1f"/>' % (cx, cy, (r + ri) / 2, col, r - ri))
        else:
            x0, y0, x1, y1 = cx + r * math.cos(a0), cy + r * math.sin(a0), cx + r * math.cos(a1), cy + r * math.sin(a1)
            xi1, yi1, xi0, yi0 = cx + ri * math.cos(a1), cy + ri * math.sin(a1), cx + ri * math.cos(a0), cy + ri * math.sin(a0)
            big = 1 if a1 - a0 > math.pi else 0
            parts.append('<path d="M%.1f %.1f A%.1f %.1f 0 %d 1 %.1f %.1f L%.1f %.1f A%.1f %.1f 0 %d 0 %.1f %.1f Z" fill="%s"><title>%s : %.1f %%</title></path>'
                         % (x0, y0, r, r, big, x1, y1, xi1, yi1, ri, ri, big, xi0, yi0, col, html.escape(lab), 100 * frac))
        leg.append('<li><i style="background:%s"></i>%s <b>%.0f %%</b> <small>(%d)</small></li>' % (col, html.escape(lab), 100 * frac, v))
        a0 = a1
    return '<svg viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-label="Camembert">%s</svg>' % (size, size, size, size, "".join(parts)), "<ul>" + "".join(leg) + "</ul>"


def build(D, press, esc, base):
    labels = D.get("labels", {})
    tot_p = sum(r[2] for r in D["totals"])
    tot_w = sum(r[3] for r in D["totals"])
    days = sorted({r[0] for r in D["totals"]})
    hours_by_day = defaultdict(set)
    pass_by_day = defaultdict(int)
    for d, h, p, w in D["totals"]:
        hours_by_day[d].add(h)
        pass_by_day[d] += p
    topic_tot, topic_day = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for d, h, t, n in D["topics"]:
        topic_tot[t] += n
        topic_day[d][t] += n
    fam_tot = defaultdict(int)
    for d, h, f, n in D["families"]:
        fam_tot[f] += n
    maj = D.get("updated", "")[:16].replace("T", " à ")
    # camemberts
    t_items = [(labels.get(t, t), n, TCOL.get(t, "#6b7480")) for t, n in sorted(topic_tot.items(), key=lambda x: -x[1])]
    f_items = [(C.FAMILIES[f][0], n, FCOL[i % len(FCOL)]) for i, (f, n) in enumerate(sorted(((f, n) for f, n in fam_tot.items() if f in C.FAMILIES), key=lambda x: -x[1]))]
    s1, l1 = pie(t_items)
    s2, l2 = pie(f_items)
    # évolution par jour : barres empilées
    order = [t for t, _ in sorted(topic_tot.items(), key=lambda x: -x[1])]
    st = []
    for d in sorted(days, reverse=True)[:14]:
        tt = float(sum(topic_day[d].values())) or 1.0
        bar = "".join('<i style="width:%.2f%%;background:%s" title="%s : %.0f %%"></i>' % (100 * topic_day[d][t] / tt, TCOL.get(t, "#6b7480"), html.escape(labels.get(t, t)), 100 * topic_day[d][t] / tt)
                      for t in order if topic_day[d][t] > 0)
        st.append('<div class="st"><span>%s</span><div class="bar">%s</div><span>%d passages</span></div>' % (esc(date_fr(d)), bar, pass_by_day[d]))
    leg = '<div class="lg">' + "".join('<span><i style="background:%s"></i>%s</span>' % (TCOL.get(t, "#6b7480"), esc(labels.get(t, t))) for t in order) + "</div>"
    # tableau jour par jour + presse
    rows = []
    for d in sorted(days, reverse=True)[:14]:
        mob = 100.0 * topic_day[d].get("mobilisation", 0) / (sum(topic_day[d].values()) or 1)
        rows.append("<tr><td>%s</td><td>%d h</td><td>%d</td><td>%.1f %%</td><td>%s</td></tr>" % (
            esc(date_fr(d)), len(hours_by_day[d]), pass_by_day[d], mob, ("{:,}".format(press[d]).replace(",", " ") if d in press else "–")))
    table = ('<div class="tw"><table><thead><tr><th>Jour</th><th>Heures couvertes</th><th>Passages</th><th>Part « manifestations et mobilisation »</th><th>Titres de presse (ça brûle)</th></tr></thead><tbody>%s</tbody></table></div>' % "".join(rows))
    top1 = max(topic_tot.items(), key=lambda x: x[1]) if topic_tot else None
    lead = ("Sur la période, le sujet le plus présent est « %s » (%d %% des passages)." % (labels.get(top1[0], top1[0]), round(100.0 * top1[1] / (sum(topic_tot.values()) or 1)))) if top1 else ""
    body = (
        '<div class="tv"><h1>De quoi parle franceinfo, et avec quels mots ?</h1>'
        '<p class="lead">Toutes les trois heures, on lit les sous-titres du direct de franceinfo, on découpe en passages d\'une quarantaine de mots et on classe chaque passage : de quoi il parle, et quels mots il emploie. '
        "<strong>Aucun texte n'est publié</strong> : seuls des comptages sont publiés. " + lead + "</p>"
        '<div class="kpi"><div><b>%s</b>passages classés</div><div><b>%s</b>mots lus</div><div><b>%d</b>jour(s)</div><div><b>%s</b>dernière mise à jour</div></div>'
        % (f"{tot_p:,}".replace(",", " "), f"{tot_w:,}".replace(",", " "), len(days), esc(maj or "–"))
        + '<h2>De quoi on parle</h2><p>Chaque passage reçoit un seul sujet. Le classement est fait par un petit programme entraîné sur 330 passages étiquetés à la main : il se trompe environ une fois sur cinq (82 % de bonnes réponses sur des passages qu\'il n\'a jamais vus).</p>'
        '<div class="card"><div class="row">' + s1 + "<div>" + l1 + "</div></div></div>"
        "<h2>Avec quels mots</h2><p>Mêmes listes de mots que sur la page <a href=\"" + base + "/cadrage\">« Les mots des journaux »</a> : désordre, autorités, voix des manifestants… Un passage peut contenir plusieurs de ces champs, donc la part est celle des mentions, pas des passages.</p>"
        '<div class="card"><div class="row">' + s2 + "<div>" + l2 + "</div></div></div>"
        "<h2>Jour par jour</h2><p>La part de chaque sujet, chaque jour. Le premier jour est partiel : la collecte a commencé en cours de journée.</p>"
        '<div class="card">' + leg + "".join(st) + "</div>"
        "<h2>Face à la presse</h2><p>Les titres de presse relevés par ça brûle le même jour (manifestations, blocages, grèves) donnent un repère pour comparer l'attention de la télé à celle de la presse en ligne. Ce ne sont pas les mêmes mesures : on ne compare que les tendances.</p>"
        + table
        + "<h2>Méthode et limites</h2><ul>"
        "<li><strong>Source.</strong> Les sous-titres du direct de franceinfo sur france.tv, qui sont générés automatiquement : ils contiennent des erreurs (noms propres, mots mal reconnus).</li>"
        "<li><strong>Une seule chaîne.</strong> BFM TV, CNews et LCI ne publient pas leurs sous-titres dans un flux lisible : on ne peut pas comparer plusieurs chaînes.</li>"
        "<li><strong>Trous possibles.</strong> Le direct ne garde que 4 heures de sous-titres, et la collecte passe toutes les 3 heures : si elle est interrompue plus longtemps, il manque des heures (la colonne « heures couvertes » le montre).</li>"
        "<li><strong>Classement des sujets.</strong> Un classifieur maison (Bayes naïf) entraîné sur 330 passages, plus des règles pour la santé, le sport et les transports, faute d'exemples. Il est moins fiable pour l'économie et la justice (peu d'exemples).</li>"
        "<li><strong>Champs lexicaux.</strong> Mêmes listes de mots que la page « cadrage » (publiées avec leur méthode), appliquées à des passages parlés, pas à des titres : les chiffres ne sont pas comparables à ceux des titres.</li>"
        "<li><strong>Conservation du texte.</strong> Le texte est lu, classé puis oublié, sauf pour les passages qui citent un candidat à la présidentielle : ceux-là sont gardés en privé, sept jours sur le serveur (avec une copie de sauvegarde privée sur un disque de l'éditeur), uniquement pour recalculer et contrôler les classements. Ils ne sont ni publiés, ni communiqués, ni cités. Seuls des comptages par heure sont gardés durablement et publiés.</li>"
        "<li><strong>Ce que ça ne mesure pas.</strong> Ni le temps d'antenne exact ni l'importance éditoriale : un passage est un bloc d'environ 40 mots de parole.</li></ul>"
        '<p class="meta">Données agrégées sous licence <a href="https://creativecommons.org/licenses/by-nc-sa/4.0/deed.fr" rel="license noopener">CC BY-NC-SA 4.0</a> : libres pour la recherche, accord préalable pour un usage commercial. '
        'Mention : « De quoi parle franceinfo, ça brûle, oraclemarin.fr/cabrule/franceinfo ». <a href="mailto:ADRESSE_DE_CONTACT">Contact</a>.</p></div>')
    return CSS, body
