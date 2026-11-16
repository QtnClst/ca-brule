"""Morts au travail : recensement de Matthieu Lépine (« Accident du travail : silence des ouvriers meurent »),
lu sur son compte Bluesky public. Un post de recensement ressemble à :
  « Accident du travail - @X - @Y - 157 morts recensés en 2026  Pont-à-Mousson (54) : un ouvrier de 56 ans… »
On garde la commune, le département, le compteur, la date et le lien vers l'article. Les noms des victimes sont retirés.
"""
import json
import re
import time
import urllib.parse
import urllib.request

ACTOR = "accidentdutravail.bsky.social"
API = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?"
RX = re.compile(r"(\d+)\s*morts?\s+recens\w*\s+en\s+(\d{4})\s*(.+?)\s*\(([0-9]{2,3}|2[AaBb])\)\s*:\s*(.+)", re.S | re.I)
# Prénom Nom (2 à 3 mots capitalisés) suivi d'une virgule, d'un âge ou de « âgé » : nom de victime
NAME_RX = re.compile(r"\b(?:[A-ZÀ-Ý][a-zà-ÿ'’]+(?:-[A-ZÀ-Ý][a-zà-ÿ'’]+)?)(?:\s+(?:de\s+|Le\s+|La\s+|Mc|O’)?[A-ZÀ-Ý][\wà-ÿ'’-]+){1,2}"
                     r"(?=\s*,|\s+âgée?|\s*\(\d+\s*ans\)|\s+\d+\s*ans|\s+a\s+(?:perdu|été|trouvé)|\s+est\s+(?:décédée?|mort|morte))")
ROLE_RX = re.compile(r"\b(le capitaine|le maître d'équipage|le maître d’équipage|le patron|le skipper|le gérant|l'agriculteur|l’agriculteur)\s+(?=[A-ZÀ-Ý])")
KEEP = {"Saint", "Sainte", "Mont", "Port", "Pont", "Val", "La", "Le", "Les"}


def _feed(timeout=30):
    posts, cur = [], None
    for _ in range(30):  # ~3000 posts max, largement assez
        q = {"actor": ACTOR, "limit": 100, "filter": "posts_no_replies"}
        if cur:
            q["cursor"] = cur
        req = urllib.request.Request(API + urllib.parse.urlencode(q), headers={"User-Agent": "ca-brule/1.0 (+https://oraclemarin.fr/cabrule/)"})
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
        for f in d.get("feed", []):
            if "reason" in f:  # reposts
                continue
            p = f["post"]
            e = p.get("embed") or {}
            ext = (e.get("external") or (e.get("media") or {}).get("external") or {})
            posts.append({"uri": p["uri"], "date": p["record"].get("createdAt", ""), "text": p["record"].get("text", ""),
                          "link": ext.get("uri", ""), "facets": p["record"].get("facets", [])})
        cur = d.get("cursor")
        if not cur or not d.get("feed"):
            break
    return posts


def _post_url(uri):
    # at://did:plc:xxx/app.bsky.feed.post/rkey -> https://bsky.app/profile/<handle>/post/rkey
    return "https://bsky.app/profile/%s/post/%s" % (ACTOR, uri.rsplit("/", 1)[-1])


def _link(post):
    if post["link"]:
        return post["link"]
    for fc in post["facets"]:
        for ft in fc.get("features", []):
            if ft.get("uri"):
                return ft["uri"]
    return _post_url(post["uri"])


def mask(text):
    # « le capitaine Prénom Nom » : le titre reste, le nom part
    text = re.sub(ROLE_RX.pattern + r"[A-ZÀ-Ý][\wà-ÿ'’-]+(?:\s+(?:Le\s+|La\s+|de\s+)?[A-ZÀ-Ý][\wà-ÿ'’-]+){0,2}",
                  lambda m: m.group(1), text)
    def rep(m):
        if m.group(0).split()[0] in KEEP:
            return m.group(0)
        return "[nom retiré]"
    text = NAME_RX.sub(rep, text)
    text = re.sub(r"(?:\[nom retiré\]\s*(?:,|et)?\s*){2,}", "[noms retirés] ", text)
    return text


def clean(desc):
    desc = re.sub(r"\s+", " ", desc)
    desc = re.sub(r"\s*(?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)+/\S*\s*$", "", desc)  # lien tronqué en fin de post
    desc = re.sub(r"\s*(?:https?://)?(?:www\.)?[\w-]+(?:\.[\w-]+)+/\S*…?", " ", desc)
    return mask(desc).strip(" .") + "."


def locate(ville, dep, full, norm):
    dep = dep.upper()
    deps = {"2A", "2B"} if dep == "20" else {dep}
    v = re.split(r"\s*[,/]\s*|\s+et\s+|\s*\(", ville)[0].strip()
    v = re.sub(r"^(?:à|a|au|en)\s+", "", v, flags=re.I)
    n = norm(v)
    n2 = re.sub(r"^(?:le|la|les|l)\s+", "", n)
    pool = [c for c in full if c["dep"] in deps]
    for test in (lambda c: c["n"] == n,
                 lambda c: re.sub(r"^(?:le|la|les|l)\s+", "", c["n"]) == n2,
                 lambda c: c["n"].startswith(n + " ") or c["n"].startswith(n2 + " "),
                 lambda c: (" " + n2 + " ") in (" " + c["n"] + " ") and len(n2) > 4):
        hits = [c for c in pool if test(c)]
        if hits:
            return max(hits, key=lambda c: c["pop"])
    # département erroné dans le post : nom unique en France
    hits = [c for c in full if c["n"] == n]
    if len(hits) == 1:
        return hits[0]
    return None


def fetch(year, full, norm):
    items = []
    for p in _feed():
        m = RX.search(p["text"])
        if not m or int(m.group(2)) != year:
            continue
        ville, dep, desc = re.sub(r"\s+", " ", m.group(3)).strip(), m.group(4), m.group(5)
        c = locate(ville, dep, full, norm)
        try:
            ts = time.mktime(time.strptime(p["date"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        except Exception:
            continue
        age = re.search(r"\b(\d{1,2})\s*ans\b", desc)
        items.append({"ts": ts, "rank": int(m.group(1)),
                      "lat": c["lat"] if c else None, "lon": c["lon"] if c else None,
                      "place": c["nom"] if c else ville, "dep": c["dep"] if c else dep.upper(),
                      "age": (age.group(1) + " ans") if age else "", "text": clean(desc)[:420],
                      "url": _link(p), "post": _post_url(p["uri"])})
    # un post peut compter plusieurs décès (ex. naufrage) : écart avec le compteur précédent
    items.sort(key=lambda i: i["rank"])
    prev = 0
    for i in items:
        i["n"] = max(1, i["rank"] - prev) if prev else 1
        prev = i["rank"]
    return items
