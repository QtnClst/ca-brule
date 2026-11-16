#!/usr/bin/env python3
"""Statistiques de visites de ça brûle, sans cookie ni traceur : lit les logs nginx et range un résumé par jour
(heure de Paris) dans /opt/ca-brule/history.db, table `visits`. Idempotent : à lancer toutes les heures.
Usage : visits.py            (met à jour la base)
        visits.py --report   (affiche les jours enregistrés)"""
import re, glob, gzip, json, sqlite3, sys, collections
from datetime import datetime
from urllib.parse import urlparse, parse_qs
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Paris")
except Exception:
    TZ = None

DB = "/opt/ca-brule/history.db"
LOG = re.compile(r'(\S+) \S+ \S+ \[([^\]]+)\] "(\S+) (\S+) [^"]*" (\d+) (\S+) "([^"]*)" "([^"]*)"')
BOT = re.compile(r'bot|crawl|spider|slurp|bing|google|yandex|baidu|duckduck|facebookexternalhit|twitter|whatsapp|telegram|discord|slack|preview|curl|python|wget|go-http|scrapy|headless|monitor|uptime|gpt|claude|perplexity|anthropic|openai|ahrefs|semrush|mj12|dotbot|petal|bytespider|amazon|archive|http-client|okhttp|axios|java/|libwww|check', re.I)
ASSET = re.compile(r'\.(js|css|png|ico|webmanifest|woff2?|map|txt|xml|json|svg|jpg|webp)(\?|$)|^/cabrule/(api|go|static|lib|widget)/')
SRC = [("reddit", "reddit"), ("google", "google"), ("bing", "bing"), ("duckduckgo", "duckduckgo"), ("chatgpt", "chatgpt"),
       ("perplexity", "perplexity"), ("claude", "claude"), ("gemini", "gemini"), ("facebook", "facebook"), ("instagram", "instagram"),
       ("snapchat", "snapchat"), ("whatsapp", "whatsapp"), ("discord", "discord"), ("telegram", "telegram"), ("t.co", "twitter"),
       ("twitter", "twitter"), ("x.com", "twitter"), ("tiktok", "tiktok"), ("bsky", "bluesky"), ("mastodon", "mastodon"),
       ("github", "github"), ("ecosia", "ecosia"), ("qwant", "qwant"), ("yandex", "yandex")]


# Robots déguisés en navigateurs (repérés le 12/11/2026) : plages chinoises et Tencent Cloud qui tournent
# (et proxys de centre de données W3 Logistics 149.232) sur des centaines d'adresses, ~6 pages chacune avec un referer interne ; plus nos propres tests (127.0.0.1).
SCRAPE_IPS = ("180.153.", "140.206.", "112.65.", "43.134.", "43.135.", "43.153.", "43.159.", "49.7.", "43.1", "49.232.", "49.233.", "82.156.", "82.157.", "101.43.", "149.232.", "127.0.0.1")


def source(ref, path, ua):
    q = parse_qs(urlparse(path).query).get("utm_source")
    if q:
        return "utm:" + q[0].lower()[:30]
    if ref in ("", "-"):
        return "direct-mobile" if re.search(r'iPhone|Android|Mobile', ua) else "direct-ordi"
    h = (urlparse(ref).hostname or "").lower()
    if h.endswith("oraclemarin.fr"):
        return "interne"
    for k, n in SRC:
        if k in h:
            return n
    return "autre:" + h[:30]


def parse():
    for f in sorted(glob.glob("/var/log/nginx/access.log*")):
        op = gzip.open if f.endswith(".gz") else open
        try:
            with op(f, "rt", errors="replace") as fh:
                for l in fh:
                    m = LOG.match(l)
                    if not m or not m.group(4).startswith("/cabrule"):
                        continue
                    ip, ts, meth, path, st, size, ref, ua = m.groups()
                    d = datetime.strptime(ts, "%d/%b/%Y:%H:%M:%S %z")
                    d = d.astimezone(TZ) if TZ else d
                    yield d, ip, meth, path, int(st), ref, ua
        except Exception as e:
            print("skip", f, e, file=sys.stderr)


def main():
    db = sqlite3.connect(DB)
    db.execute("""CREATE TABLE IF NOT EXISTS visits(day TEXT PRIMARY KEY, pages_h INTEGER, ips_h INTEGER, pages_bot INTEGER,
                  api_ips INTEGER, sources TEXT, top_pages TEXT, devices TEXT, hours TEXT, updated TEXT)""")
    if "--report" in sys.argv:
        print("jour        pages_h  visiteurs(IP)  api(IP)  bots   sources")
        for r in db.execute("SELECT day,pages_h,ips_h,api_ips,pages_bot,sources FROM visits ORDER BY day"):
            print(r[0], "%7d %12d %9d %6d  " % (r[1], r[2], r[3], r[4]), r[5])
        return
    days = collections.defaultdict(lambda: {"h": [], "b": 0, "api": set(), "hours": collections.Counter()})
    for d, ip, meth, path, st, ref, ua in parse():
        k = d.strftime("%Y-%m-%d")
        if path.startswith("/cabrule/api/events"):
            days[k]["api"].add(ip)
        if meth != "GET" or st != 200 or ASSET.search(path):
            continue
        if BOT.search(ua) or ua in ("", "-") or ip.startswith(SCRAPE_IPS):
            days[k]["b"] += 1
        else:
            days[k]["h"].append((ip, path, ref, ua, d.hour))
    for k, v in days.items():
        h = v["h"]
        old = db.execute("SELECT pages_h FROM visits WHERE day=?", (k,)).fetchone()
        if old and old[0] > len(h):
            continue  # les logs de ce jour ont déjà été en partie effacés : on garde la version la plus complète
        src = collections.Counter(source(r, p, u) for _, p, r, u, _ in h)
        top = collections.Counter(p.split("?")[0] for _, p, _, _, _ in h)
        dev = collections.Counter("mobile" if re.search(r'iPhone|Android|Mobile', u) else "ordi" for _, _, _, u, _ in h)
        hrs = collections.Counter(x[4] for x in h)
        db.execute("INSERT OR REPLACE INTO visits VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (k, len(h), len({x[0] for x in h}), v["b"], len(v["api"]), json.dumps(dict(src.most_common(12)), ensure_ascii=False),
                    json.dumps(dict(top.most_common(12)), ensure_ascii=False), json.dumps(dict(dev)), json.dumps(dict(sorted(hrs.items()))),
                    datetime.now(TZ).isoformat(timespec="seconds") if TZ else ""))
    db.commit()
    print("jours mis à jour :", ", ".join(sorted(days)))


main()
