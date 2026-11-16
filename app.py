"""ça brûle — carte des contestations en France d'après la presse (flux RSS de la presse).
Lancer : python app.py  puis ouvrir http://localhost:8765
"""
import urllib.error
import sqlite3
import hs as HSX  # tri des articles : hors-sujet, anciens, comptes rendus tardifs (voir hs.py)
import cadrage as CAD  # vocabulaire des titres par type de média (page /cadrage)
import cadrage_page as CADP
import cadrage2 as CAD2
import tv_page as TVP  # page /franceinfo : sujets et mots du direct de franceinfo (comptages poussés depuis le PC)
from datetime import datetime, timedelta, timezone
import base64, collections, hashlib, html, json, os, re, threading, time, unicodedata, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 8765))
REFRESH = 600  # secondes entre deux cycles de collecte
CITY_CHUNK = 100  # villes interrogées par cycle (rotation : les 300 sont couvertes en 3 cycles)
TOP_CITIES = 300  # villes interrogées individuellement
CITY_TERMS = '(blocus OR blocage OR manifestation OR grève OR rassemblement OR mobilisation OR manifestants)'
MASTO_TAGS = ["blocus", "manifestation", "blocage", "grève", "mobilisation"]
UA = {"User-Agent": "Mozilla/5.0 (ca-brule prototype)"}

QUERIES = ["manifestation", "manifestants", "blocage", "grève", "rassemblement",
           "mobilisation", "agriculteurs colère", "émeutes", "piquet de grève", "occupation lycée"]
KEYWORDS = re.compile(r"blocus|heurts|mouvement lyc[ée]en|lyc[ée]ens? (?:mobilis|bloqu)|manifest|manif\b|blocage|bloqu|gr[eè]ve|rassembl|mobilis|piquet|[eé]meute|"
                      r"barrages? (?:filtrants?|routiers?|de (?:manifestants|tracteurs|agriculteurs|gendarmes|police|cars))|occupation|occup[ée]s? (?:par|depuis)|march[ea] (?:de|pour|contre)|colère|syndicat|intersyndicale", re.I)
EXCLUDE = re.compile(r"\b(?:football|ligue 1|rugby|top 14|mercato|horoscope|salon|exposition|festival|octobre rose|concert|spectacle)\b", re.I)
# Expressions qui contiennent un mot-clé sans parler de contestation : on les retire avant de chercher les mots-clés.
#  « appel à manifestation d'intérêt » (marché public), « rassembler » au sens d'unir (slogans politiques), « rassembleur ».
NOISE = re.compile(r"appels? [àa] manifestations? d.int[ée]r[êe]t|manifestations? d.int[ée]r[êe]t|(?<!se )(?<!s')\brassembler\b|\brassembleu[rs]e?s?\b", re.I)
STOP = {"pour", "bon", "meme", "ainsi", "plaine", "sur", "mars", "juin", "pres", "tout", "ceux",
        "avant", "apres", "gare", "lors", "est", "les", "des", "une", "grand", "saint", "ville",
        "vienne", "force", "loire", "france", "mobilisation", "colere", "manifestation", "gréve",
        "retraites", "budget", "paris 2024", "ici", "eux", "sens", "ligne", "tours de", "bar", "fort",
        "matignon", "syndicat", "le syndicat", "leon", "caux", "bercy", "assemblee", "senat", "elysee", "doubs", "jura", "moselle", "var", "gard", "aisne", "marne", "somme", "nord", "meuse", "orne", "indre", "cher", "lot", "gers", "ain", "eure", "oise", "tarn", "aude", "cote d or", "le", "la", "ce", "va", "ay", "do", "bu", "ste", "cour", "pont", "port", "sète"} - {"sète"}


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[-'’]", " ", s).strip()


FULL = []  # toutes les communes (toutes tailles) : recherche et scan de près


def load_communes():
    path = os.path.join(HERE, "communes.json")
    if not os.path.exists(path):
        url = "https://geo.api.gouv.fr/communes?fields=nom,centre,population,codeDepartement&format=json&geometry=centre"
        data = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
        open(path, "wb").write(data)
    idx = {}
    for c in json.load(open(path, encoding="utf-8")):
        pop = c.get("population") or 0
        if c.get("centre"):
            FULL.append({"nom": c["nom"], "lon": c["centre"]["coordinates"][0], "lat": c["centre"]["coordinates"][1],
                         "pop": pop, "dep": c.get("codeDepartement"), "code": c.get("code"), "n": norm(c["nom"])})
        if pop < 1500 or not c.get("centre"):
            continue
        k = norm(c["nom"])
        if k in STOP:
            continue
        rec = {"nom": c["nom"], "lon": c["centre"]["coordinates"][0],
               "lat": c["centre"]["coordinates"][1], "pop": pop, "dep": c.get("codeDepartement")}
        if k not in idx or pop > idx[k]["pop"]:
            idx[k] = rec
        # « au Mans », « du Havre », « à La Rochelle » : alias sans l'article
        m = re.match(r"(?:le|la|les|l) (.+)", k)
        if m and pop >= 5000 and m.group(1) not in STOP:
            a = m.group(1)
            if a not in idx or pop > idx[a]["pop"]:
                idx[a] = rec
    return idx


COMMUNES = {}
STATE = {"events": [], "updated": None, "status": "démarrage…"}
LOCK = threading.Lock()


def geocode(title):
    """Cherche la plus longue suite de mots à majuscule correspondant à une commune."""
    # on tente toutes les fenêtres de 1 à 5 tokens commençant par une majuscule
    toks = re.findall(r"[\wÀ-ÿ'’-]+", title)
    best = None
    for i, t in enumerate(toks):
        if not t[0].isupper():
            continue
        for n in range(min(5, len(toks) - i), 0, -1):
            cand = " ".join(toks[i:i + n])
            c = COMMUNES.get(norm(cand))
            if c:
                # un mot unique en début de titre est peu fiable (majuscule de phrase)
                if n == 1 and i == 0 and c["pop"] < 30000:
                    continue
                if not best or n > best[0] or (n == best[0] and c["pop"] > best[1]["pop"]):
                    best = (n, c)
                break
    return best[1] if best else None


def geocode_all(title):
    """Toutes les communes citées dans un titre (« Hendaye, Biarritz, Bayonne : plusieurs actions… »), sans chevauchement."""
    toks = re.findall(r"[\wÀ-ÿ'’-]+", title)
    out, used, i = [], set(), 0
    while i < len(toks):
        t = toks[i]
        if not t[0].isupper() or i in used:
            i += 1
            continue
        hit = None
        for n in range(min(5, len(toks) - i), 0, -1):
            c = COMMUNES.get(norm(" ".join(toks[i:i + n])))
            if c and not (n == 1 and i == 0 and c["pop"] < 30000 and not re.match(r"\W*%s\s*," % re.escape(toks[0]), title)):
                hit = (n, c)
                break
        if hit:
            used.update(range(i, i + hit[0]))
            if all(o["nom"] != hit[1]["nom"] or o["dep"] != hit[1]["dep"] for o in out):
                out.append(hit[1])
            i += hit[0]
        else:
            i += 1
    return out


LIST_SEP = re.compile(r"^\s*(?:,|/|\bet\b|\bou\b)\s*$", re.I)


def list_communes(title):
    """Communes énumérées de façon consécutive (« Hendaye, Biarritz et Bayonne ») : le plus grand groupe d'au moins deux, sinon []."""
    toks = [(m.group(), m.start(), m.end()) for m in re.finditer(r"[\wÀ-ÿ'’-]+", title)]
    hits, i = [], 0
    while i < len(toks):
        if not toks[i][0][0].isupper():
            i += 1
            continue
        hit = None
        for n in range(min(5, len(toks) - i), 0, -1):
            c = COMMUNES.get(norm(" ".join(t[0] for t in toks[i:i + n])))
            if c and not (n == 1 and i == 0 and c["pop"] < 30000 and not re.match(r"\W*%s\s*," % re.escape(toks[0][0]), title)):
                hit = (n, c)
                break
        if hit:
            hits.append((hit[1], toks[i][1], toks[i + hit[0] - 1][2]))
            i += hit[0]
        else:
            i += 1
    groups, cur = [], []
    for h in hits:
        if cur and LIST_SEP.match(title[cur[-1][2]:h[1]]):
            cur.append(h)
        else:
            if cur:
                groups.append(cur)
            cur = [h]
    if cur:
        groups.append(cur)
    best = max(groups, key=len) if groups else []
    out = []
    for h in best:
        if all((o["nom"], o["dep"]) != (h[0]["nom"], h[0]["dep"]) for o in out):
            out.append(h[0])
    return out if len(out) >= 2 else []


def _items(url, ua=None):
    req = urllib.request.Request(url, headers={"User-Agent": ua or UA["User-Agent"]})
    return ET.fromstring(urllib.request.urlopen(req, timeout=30).read()).iter("item")


def fetch_google(q, when="1d"):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:{when}" if when else q, "hl": "fr", "gl": "FR", "ceid": "FR:fr"})
    for it in _items(url):
        title = (it.findtext("title") or "").strip()
        src = it.findtext("source") or ""
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            continue
        yield {"title": title, "src": src, "link": it.findtext("link"), "ts": ts}


def fetch_bing(q):
    url = "https://www.bing.com/news/search?" + urllib.parse.urlencode({"q": q, "format": "rss", "setmkt": "fr-FR", "qft": 'interval="7"'})
    for it in _items(url):
        title = (it.findtext("title") or "").strip()
        link = it.findtext("link") or ""
        try:
            ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
        except Exception:
            continue
        if time.time() - ts > 86400 or ts > time.time() + 3600:
            continue
        real = urllib.parse.parse_qs(urllib.parse.urlparse(link).query).get("url", [link])[0]
        src = urllib.parse.urlparse(real).netloc.replace("www.", "")
        yield {"title": title, "src": src, "link": real, "ts": ts}


def fetch_mastodon(tag):
    url = f"https://mastodon.social/api/v1/timelines/tag/{urllib.parse.quote(tag)}?limit=40"
    for st in json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30)):
        if st.get("language") not in ("fr", None):
            continue
        text = html.unescape(re.sub(r"<[^>]+>", " ", st.get("content", ""))).strip()
        ts = time.mktime(time.strptime(st["created_at"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        if time.time() - ts > 86400:
            continue
        yield {"title": re.sub(r"\s+", " ", text)[:200], "src": "Mastodon", "link": st["url"], "ts": ts}



# --- Sources de secours / complémentaires : flux presse directs (sans moteur de recherche) et GDELT ---
ICI_STATIONS = ("107-1 alsace armorique auxerre azur bearn belfort-montbeliard berry besancon bourgogne breizh-izel "
                "champagne-ardenne cotentin creuse drome-ardeche elsass gard-lozere gascogne gironde herault isere "
                "la-rochelle limousin loire-ocean lorraine-nord maine mayenne nord normandie-caen normandie-rouen "
                "orleans pays-basque pays-d-auvergne pays-de-savoie perigord picardie poitou provence rcfm roussillon "
                "saint-etienne-loire sud-lorraine toulouse touraine vaucluse").split()
FEEDS = ([f"https://www.ici.fr/rss/{st}/a-la-une.xml" for st in ICI_STATIONS] + [
    "https://www.ici.fr/rss/a-la-une.xml", "https://www.ladepeche.fr/rss.xml", "https://www.sudouest.fr/rss.xml",
    "https://www.letelegramme.fr/rss.xml", "https://www.ouest-france.fr/rss/une", "https://actu.fr/rss.xml",
    "https://www.lemonde.fr/rss/en_continu.xml", "https://www.francetvinfo.fr/titres.rss",
    "https://www.20minutes.fr/feeds/rss-une.xml", "https://www.bfmtv.com/rss/news-24-7/",
    "https://www.lefigaro.fr/rss/figaro_actualites.xml", "https://www.lexpress.fr/rss/alaune.xml",
    "https://www.liberation.fr/arc/outboundfeeds/rss-all/collection/accueil-une/?outputType=xml"])
FEED_NAMES = {"www.ici.fr": "ici", "www.ladepeche.fr": "La Dépêche", "www.sudouest.fr": "Sud Ouest",
              "www.letelegramme.fr": "Le Télégramme", "www.ouest-france.fr": "Ouest-France", "actu.fr": "actu.fr",
              "www.lemonde.fr": "Le Monde", "www.francetvinfo.fr": "franceinfo", "www.20minutes.fr": "20 Minutes",
              "www.bfmtv.com": "BFMTV", "www.lefigaro.fr": "Le Figaro", "www.lexpress.fr": "L'Express",
              "www.liberation.fr": "Libération"}
GDELT_QUERIES = ["blocus OR blocage OR manifestation OR grève", "manifestants OR rassemblement OR mobilisation OR émeutes"]
GDELT_LOCK = threading.Lock()
GDELT_LAST = [0.0]
GDELT_BLOCK = [0.0]  # suspension après un 429


_MOIS = {"jan": 1, "fev": 2, "feb": 2, "mar": 3, "avr": 4, "apr": 4, "mai": 5, "may": 5, "jui": 6, "jun": 6, "jul": 7,
         "aou": 8, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def feed_ts(v):
    """Date d'un article de flux : RSS standard, ISO 8601, ou « 30 Sep. 2026 » (sans heure : midi, heure de Paris)."""
    if not v:
        return None
    try:
        return parsedate_to_datetime(v).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    m = re.match(r"\s*(\d{1,2})\s+([A-Za-zéûèÉ]+)\.?\s+(\d{4})", v)
    if m:
        mo = _MOIS.get(norm(m.group(2))[:3])
        if mo:
            try:
                return datetime(int(m.group(3)), mo, int(m.group(1)), 12, 0, tzinfo=TZ).timestamp()
            except Exception:
                return None
    return None


def fetch_feed(url):
    host = urllib.parse.urlparse(url).netloc
    src = FEED_NAMES.get(host, host)
    for it in _items(url, ua="Mozilla/5.0"):
        title = html.unescape((it.findtext("title") or "").strip())
        ts = feed_ts(it.findtext("pubDate"))
        if ts is None or time.time() - ts > 86400 or ts > time.time() + 3600:
            continue
        yield {"title": title, "src": src, "link": it.findtext("link") or "", "ts": ts}


def fetch_gdelt(q):
    with GDELT_LOCK:  # GDELT n'accepte qu'une requête toutes les 5 s
        if time.time() < GDELT_BLOCK[0]:
            return
        wait = 6.5 - (time.time() - GDELT_LAST[0])
        if wait > 0:
            time.sleep(wait)
        GDELT_LAST[0] = time.time()
        url = "https://api.gdeltproject.org/api/v2/doc/doc?" + urllib.parse.urlencode(
            {"query": f"({q}) sourcecountry:france sourcelang:french", "mode": "artlist", "format": "json",
             "maxrecords": "100", "timespan": "1d", "sort": "datedesc"})
        try:
            data = json.loads(http_text(url, 40))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                GDELT_BLOCK[0] = time.time() + 1800
                print("GDELT limite les requêtes : pause de 30 min")
                return
            raise
    for a in data.get("articles", []):
        try:
            ts = time.mktime(time.strptime(a["seendate"], "%Y%m%dT%H%M%SZ")) - time.timezone
        except Exception:
            continue
        yield {"title": a.get("title", ""), "src": a.get("domain", "gdelt"), "link": a.get("url", ""), "ts": ts}


NEW_FEEDS = {
    # presse régionale
    "https://www.dna.fr/rss": "Dernières Nouvelles d'Alsace", "https://www.lejsl.com/rss": "Le Journal de Saône-et-Loire",
    "https://www.lalsace.fr/rss": "L'Alsace", "https://www.leprogres.fr/rss": "Le Progrès",
    "https://www.ledauphine.com/rss": "Le Dauphiné libéré", "https://www.nicematin.com/rss": "Nice-Matin",
    "https://www.midilibre.fr/rss.xml": "Midi Libre", "https://www.bienpublic.com/rss": "Le Bien Public",
    "https://www.republicain-lorrain.fr/rss": "Le Républicain Lorrain", "https://www.vosgesmatin.fr/rss": "Vosges Matin",
    "https://www.lindependant.fr/rss.xml": "L'Indépendant", "https://www.charentelibre.fr/rss.xml": "Charente Libre",
    "https://www.estrepublicain.fr/rss": "L'Est Républicain",
    "https://france3-regions.franceinfo.fr/paris-ile-de-france/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/provence-alpes-cote-d-azur/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/bretagne/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/pays-de-la-loire/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/nouvelle-aquitaine/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/occitanie/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/auvergne-rhone-alpes/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/hauts-de-france/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/grand-est/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/normandie/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/bourgogne-franche-comte/rss": "France 3 Régions",
    "https://france3-regions.franceinfo.fr/centre-val-de-loire/rss": "France 3 Régions",
    "https://marsactu.fr/feed/": "Marsactu", "https://www.rue89lyon.fr/feed/": "Rue89 Lyon",
    "https://www.rue89strasbourg.com/feed": "Rue89 Strasbourg", "https://rue89bordeaux.com/feed/": "Rue89 Bordeaux",
    "https://www.mediacites.fr/feed/": "Médiacités",
    # presse indépendante et militante
    "https://reporterre.net/spip.php?page=backend": "Reporterre", "https://basta.media/spip.php?page=backend": "Basta!",
    "https://rapportsdeforce.fr/feed": "Rapports de Force", "https://contre-attaque.net/feed/": "Contre-Attaque",
    "https://www.humanite.fr/rss/actu.rss": "L'Humanité", "https://www.mediapart.fr/articles/feed": "Mediapart",
    # national / international
    "https://www.francetvinfo.fr/societe.rss": "franceinfo", "https://www.francetvinfo.fr/politique.rss": "franceinfo",
    "https://www.francetvinfo.fr/economie.rss": "franceinfo", "https://www.rfi.fr/fr/france/rss": "RFI",
    "https://www.france24.com/fr/france/rss": "France 24", "https://www.europe1.fr/rss.xml": "Europe 1",
    "https://www.cnews.fr/rss.xml": "CNews", "https://fr.euronews.com/rss": "Euronews",
    # presse reconnue (rubriques)
    "https://www.lemonde.fr/societe/rss_full.xml": "Le Monde", "https://www.lemonde.fr/politique/rss_full.xml": "Le Monde",
    "https://www.lemonde.fr/education/rss_full.xml": "Le Monde", "https://www.francetvinfo.fr/societe/education.rss": "franceinfo",
    "https://www.francetvinfo.fr/economie/transports.rss": "franceinfo", "https://www.lefigaro.fr/rss/figaro_actualite-france.xml": "Le Figaro",
    # presse spécialisée, régionale et outre-mer
    "https://www.letudiant.fr/rss.xml": "L'Étudiant", "https://www.cafepedagogique.net/feed/": "Café pédagogique",
    "https://www.terre-net.fr/rss": "Terre-net", "https://actu.fr/ile-de-france/rss.xml": "actu.fr",
    "https://la1ere.francetvinfo.fr/nouvellecaledonie/rss": "La 1ère Nouvelle-Calédonie",
    # syndicats et associations : communiqués de leurs propres organisations
    "https://www.force-ouvriere.fr/spip.php?page=backend": "Force Ouvrière", "https://fsu.fr/feed/": "FSU",
    "https://www.snes.edu/feed": "SNES-FSU", "https://www.unsa.org/feed": "UNSA", "https://solidaires.org/feed/": "Solidaires",
    "https://www.ldh-france.org/feed/": "Ligue des droits de l'Homme", "https://www.cgt.fr/rss.xml": "CGT",
    "https://france.attac.org/spip.php?page=backend": "Attac", "https://www.greenpeace.fr/feed/": "Greenpeace France",
    "https://www.oxfamfrance.org/feed/": "Oxfam France",
    "https://actu.fr/normandie/rss.xml": "actu.fr",
    "https://actu.fr/bretagne/rss.xml": "actu.fr",
    "https://actu.fr/pays-de-la-loire/rss.xml": "actu.fr",
    "https://actu.fr/hauts-de-france/rss.xml": "actu.fr",
    "https://actu.fr/grand-est/rss.xml": "actu.fr",
    "https://actu.fr/auvergne-rhone-alpes/rss.xml": "actu.fr",
    "https://actu.fr/nouvelle-aquitaine/rss.xml": "actu.fr",
    "https://actu.fr/occitanie/rss.xml": "actu.fr",
    "https://actu.fr/provence-alpes-cote-d-azur/rss.xml": "actu.fr",
    "https://actu.fr/bourgogne-franche-comte/rss.xml": "actu.fr",
    "https://actu.fr/centre-val-de-loire/rss.xml": "actu.fr",
    "https://www.macommune.info/feed/": "macommune.info", "https://www.lejournaltoulousain.fr/feed/": "Le Journal Toulousain",
    "https://www.ouest-france.fr/rss/france": "Ouest-France", "https://www.liberation.fr/arc/outboundfeeds/rss-all/?outputType=xml": "Libération",
    "https://www.humanite.fr/feed": "L'Humanité",
}
for _u, _n in NEW_FEEDS.items():
    FEEDS.append(_u)
    FEED_NAMES[urllib.parse.urlparse(_u).netloc] = _n
FEED_NAMES["france3-regions.franceinfo.fr"] = "France 3 Régions"

CPPAP_FEEDS = []     # services de presse en ligne inscrits à la CPPAP dont le flux a été vérifié (fichier cppap_feeds.json)
CPPAP_DOMAINS = set()  # tous les services de presse en ligne inscrits (fichier cppap_domains.json), pour signaler la reconnaissance
try:
    CPPAP_FEEDS = json.load(open(os.path.join(HERE, "cppap_feeds.json"), encoding="utf-8"))
    CPPAP_DOMAINS = set(json.load(open(os.path.join(HERE, "cppap_domains.json"), encoding="utf-8")))
    for _f in CPPAP_FEEDS:
        if _f["feed"] not in FEEDS:
            FEEDS.append(_f["feed"])
        FEED_NAMES.setdefault(_f["host"], _f["nom"])
except Exception as _e:
    print("flux CPPAP indisponibles :", _e)


CURSOR = [0]
# Disjoncteur : si Google répond 429/503, on suspend ses requêtes (30 min, puis 1 h, 2 h… max 4 h)
BLOCK = {"until": 0, "step": 1800}


GOOGLE_ON = os.environ.get("GOOGLE_NEWS", "1") != "0"  # GOOGLE_NEWS=0 coupe Google Actualités
# Plafond dynamique (AIMD) : requêtes Google autorisées par fenêtre de 10 min. Il double de prudence à chaque 429/503
# (÷2, minimum 4) et regagne +4 par cycle sans incident (maximum 60) ; les flux RSS ne sont jamais concernés.
GCAP = {"cap": 20, "min": 4, "max": 60, "hits": 0}
GCALLS = []  # horodatages des requêtes Google de la fenêtre glissante


def google_room():
    now = time.time()
    GCALLS[:] = [t for t in GCALLS if now - t < 600]
    return max(0, GCAP["cap"] - len(GCALLS))


def blocked():
    return not GOOGLE_ON or time.time() < BLOCK["until"] or google_room() <= 0


def jobs():
    j = [(fetch_feed, u) for u in FEEDS]
    j += [(fetch_gdelt, q) for q in GDELT_QUERIES]
    if GOOGLE_ON:
        j += [(fetch_google, q) for q in QUERIES]  # d'abord les requêtes générales (les plus utiles)
    j += [(fetch_bing, q) for q in ("blocus lycées", "manifestation", "blocage", "grève", "rassemblement", "mobilisation")]
    j += [(fetch_mastodon, t) for t in MASTO_TAGS]
    top = sorted({c["nom"]: c for c in COMMUNES.values()}.values(), key=lambda c: -c["pop"])[:TOP_CITIES]
    start = CURSOR[0] % len(top)
    size = max(0, min(CITY_CHUNK, GCAP["cap"] - len(QUERIES)))  # le reste du plafond va aux villes, par rotation
    chunk = (top + top)[start:start + size]
    CURSOR[0] = (start + size) % len(top)
    if GOOGLE_ON:
        j += [(fetch_google, f'{CITY_TERMS} "{c["nom"]}"') for c in chunk]
    return j


def run_job(job):
    fn, q = job
    if fn is fetch_google and blocked():
        return []
    if fn is fetch_google:
        GCALLS.append(time.time())
    if fn is not fetch_feed:
        BUDGET.append(time.time())
    try:
        out = list(fn(q))
        if fn is fetch_google:
            BLOCK["step"] = 1800
        return out
    except urllib.error.HTTPError as e:
        if fn is fetch_bing and e.code in (403, 429, 503):
            BLOCK_BING["until"] = time.time() + BLOCK_BING["step"]
            print("Bing limite les requêtes (HTTP %d) : pause de %d min" % (e.code, BLOCK_BING["step"] // 60))
            BLOCK_BING["step"] = min(BLOCK_BING["step"] * 2, 3 * 3600)
        elif fn is fetch_google and e.code in (429, 503):
            BLOCK["until"] = time.time() + BLOCK["step"]
            GCAP["hits"] += 1
            GCAP["cap"] = max(GCAP["min"], GCAP["cap"] // 2)
            print("Google limite les requêtes (HTTP %d) : pause de %d min, plafond ramené à %d / 10 min" % (e.code, BLOCK["step"] // 60, GCAP["cap"]))
            BLOCK["step"] = min(BLOCK["step"] * 2, 4 * 3600)
        else:
            print("erreur", fn.__name__, q[:40], e)
        return []
    except Exception as e:
        print("erreur", fn.__name__, q[:40], e)
        return []
    finally:
        time.sleep(1.5)


THEMES = {  # clé -> motifs sur titre normalisé (minuscules, sans accents)
    "lyceens": r"lycee|lyceen|etudiant|universit|\bfac\b|blocus|parcoursup|college|eleves?\b|campus|scolaire|enseignant|professeur|\bprofs?\b|education nationale|rectorat",
    "agri": r"agricult|paysan|tracteur|eleveur|fnsea|coordination rurale|mercosur|viticult|pecheur|ferme\b|exploitant",
    "travail": r"greve|syndicat|intersyndicale|salarie|licenci|usine|\bcgt\b|cfdt|force ouvriere|fonctionnaire|plan social|piquet|ouvrier|emploi|intermittent|chomage|salaire",
    "transports": r"sncf|ratp|cheminot|controleur|aiguilleur|aerien|aeroport|\bbus\b|tram\b|tramway|metro\b|transports?|conducteur|trafic|peage|autoroute",
    "sante": r"hopital|hospitalier|soignant|infirmi|medecin|urgences|\bsante\b|ehpad|pharmac|chu\b",
    "ecologie": r"climat|ecolog|megabassine|bassines?|\ba69\b|extinction rebellion|soulevements|nucleaire|biodiversite|pesticide|animaux|anti.?chasse",
    "international": r"palestin|gaza|israel|ukrain|iran|liban|syrie|solidarite avec|contre la guerre|trump",
    "politique": r"budget|reforme|gouvernement|retraite|\b49\.?3\b|premier ministre|macron|matignon|assemblee|projet de loi|\bloi\b|\blfi\b|rassemblement national|ultragauche|ultradroite|\bpnf\b",
    "violences": r"brul|poubelle|pompier|caillass|jets? de|violence|emeute|heurts?\b|affrontement|lbd|lacrymo|interpell|incendi|\bfeux?\b|mortier|degradation|blesse|police|gendarm|casseur|tension",
    "securite_logement": r"logement|loyer|expulsion|squat|insecurite|narcotrafic|deal|quartier|ordures|dechets",
}
THEME_RE = {k: re.compile(v) for k, v in THEMES.items()}


def themes_of(title):
    t = norm(title)
    return [k for k, r in THEME_RE.items() if r.search(t)] or ["autre"]


STORE = {}        # clé de titre -> {"a": article, "c": commune}
LAST_FETCH = {}   # (nom, dep) -> timestamp de la dernière requête dédiée
ALL = []          # toutes les communes indexées (uniques)
BUDGET = []       # horodatages de toutes les requêtes sortantes (fenêtre glissante 10 min)
AREA_LOCK = threading.Lock()
AREA_TTL = 900
AREA_MAX_PER_CALL = 15
TOTAL_BUDGET = 450  # requêtes sortantes max par 10 min (collecte de base + à la demande)


RAW = {}  # articles de protestation sans commune reconnue (24 h) : réservoir pour la recherche locale hors ligne
RAW_MAX = 6000


DECIDED = {}  # titre normalisé -> verdict du tri (hs.py), pour ne pas reclasser à chaque cycle

# ---------------------------------------------------------------------------
# Dernier filtre : Jev (API externe) pour les titres SANS preuve nette de contestation.
# Question : contestation / rassemblement non revendicatif / hors sujet. On n'écarte que « hors sujet » avec confiance >= 0.8.
# Le verdict est mis en cache (table jev_gate de history.db) : un titre n'est envoyé qu'une fois. Si Jev est injoignable, on garde l'article.
# Seuls des titres de presse publics sont envoyés ; la clé reste côté serveur (jev-proxy local).
# ---------------------------------------------------------------------------
JEV_URL = "http://127.0.0.1:3912/decide"
JEV_KEYFILES = (os.path.join(HERE, ".jev_key"), "proxy-key.txt")
JEV_MAX_CONF = 0.8
JEV_PER_MIN = 100  # plafond d'appels (le proxy accepte 120/min)
JEV_STATE = {"fails": 0, "pause": 0.0, "key": None, "calls": []}
JEV_LOCK = threading.Lock()
JEV_Q = {"nature": {"type": "choice", "instructions": "Ce titre de presse (France) est-il un article sur une mobilisation de contestation ?",
                    "criteria": {
                        "contestation": "Manifestation, grève, blocus, blocage, rassemblement ou appel à se rassembler pour protester ou revendiquer (en cours, passé ou annoncé)",
                        "rassemblement_non_revendicatif": "Hommage, commémoration, marche solidaire ou festive, rassemblement sans revendication de protestation",
                        "hors_sujet": "Sans rapport avec une mobilisation de contestation (fait divers, sport, culture, audience TV, politique sans mobilisation, appel d'offres, international, etc.)"}}}


def _jev_key():
    if JEV_STATE["key"] is None:
        JEV_STATE["key"] = ""
        for p in JEV_KEYFILES:
            try:
                JEV_STATE["key"] = open(p).read().strip()
                break
            except Exception:
                continue
    return JEV_STATE["key"]


def jev_gate(title):
    """'ok' | 'hs' | None (inconnu : plafond atteint, Jev en pause ou clé absente → on garde l'article et on réessaiera)."""
    k = norm(title)[:120]
    try:
        with DB_LOCK:
            conn = db()
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS jev_gate(k TEXT PRIMARY KEY, verdict TEXT, conf REAL, at REAL)")
                r = conn.execute("SELECT verdict FROM jev_gate WHERE k=?", (k,)).fetchone()
            finally:
                conn.close()
        if r:
            return r[0]
    except Exception as e:
        print("jev : cache illisible", e)
    now = time.time()
    key = _jev_key()
    with JEV_LOCK:
        JEV_STATE["calls"] = [t for t in JEV_STATE["calls"] if now - t < 60]
        if not key or now < JEV_STATE["pause"] or len(JEV_STATE["calls"]) >= JEV_PER_MIN:
            return None
        JEV_STATE["calls"].append(now)
    try:
        body = json.dumps({"state": title, "questions": JEV_Q}).encode()
        req = urllib.request.Request(JEV_URL, body, {"Content-Type": "application/json", "x-proxy-key": key})
        ans = json.loads(urllib.request.urlopen(req, timeout=8).read())["answers"]["nature"]
        verdict = "hs" if ans["choice"] == "hors_sujet" and ans["confidence"] >= JEV_MAX_CONF else "ok"
    except Exception as e:
        with JEV_LOCK:
            JEV_STATE["fails"] += 1
            if JEV_STATE["fails"] >= 3:  # Jev en panne ou clé refusée : pause 10 min, on garde tout
                JEV_STATE["pause"] = time.time() + 600
                JEV_STATE["fails"] = 0
                print("jev : 3 échecs de suite, pause 10 min (%s)" % e)
        return None
    JEV_STATE["fails"] = 0
    try:
        with DB_LOCK:
            conn = db()
            try:
                conn.execute("INSERT OR REPLACE INTO jev_gate VALUES (?,?,?,?)", (k, verdict, float(ans["confidence"]), time.time()))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print("jev : cache non enregistré", e)
    return verdict


def triage(a, key):
    """((action, détail), première fois ?) : « ok », « retro » (jour de l'événement) ou « reject » (hs / ancien / jev)."""
    d = DECIDED.get(key)
    if d is not None:
        return d, False
    try:
        d = HSX.verdict(a["title"], a["ts"])
        if d[0] == "ok" and not HSX.has_evidence(a["title"]) and jev_gate(a["title"]) == "hs":
            d = ("reject", "jev")
    except Exception as e:
        print("tri : erreur", e)
        d = ("ok", None)
    if len(DECIDED) > 60000:
        DECIDED.clear()
    DECIDED[key] = d
    return d, True


def ingest(arts, hint=None):
    dirty = set()
    for a in arts:
        key = norm(a["title"])[:80]
        if key in STORE or not KEYWORDS.search(NOISE.sub(" ", a["title"])) or EXCLUDE.search(a["title"]):
            continue
        (act, det), first = triage(a, key)
        if act == "reject":
            if first:
                reject_record(a, det)
            continue
        c = geocode(a["title"])
        if hint and hint["n"] in norm(a["title"]):
            c = hint  # requête dédiée à cette commune : on lui attribue l'article si elle est citée
        if act == "retro":  # compte rendu d'un événement passé : pas sur la carte du jour, mais au jour de l'événement
            if c and first:
                retro_record(a, c, det)
                dirty.add(det)
            continue
        if c:
            a["themes"] = themes_of(a["title"])
            STORE[key] = {"a": a, "c": c}
            RAW.pop(key, None)
            for extra in list_communes(a["title"]):  # « Hendaye, Biarritz, Bayonne : … » : une entrée par commune citée
                if (extra["nom"], extra["dep"]) != (c["nom"], c["dep"]):
                    STORE[key + "|" + norm(extra["nom"]) + str(extra["dep"])] = {"a": dict(a), "c": extra}
        elif len(RAW) < RAW_MAX:
            RAW[key] = a
    today = day_of(time.time())
    for d in dirty:
        if d < today:
            try:
                finalize_day(d)
            except Exception as e:
                print("tri : refigeage impossible", d, e)


def scan_raw(c):
    """Repli sans requête externe : cherche la commune dans les articles déjà collectés mais non localisés.
    On exige le nom avec sa majuscule (« Barre » commune ≠ « barré » verbe)."""
    if len(c["n"]) < 4:
        return 0
    pat = re.compile(r"(?<![\wÀ-ÿ])%s(?![\wÀ-ÿ])" % re.escape(c["nom"]))
    pat_n = re.compile(r"(?<![a-z0-9])%s(?![a-z0-9])" % re.escape(c["n"])) if len(c["n"]) >= 8 else None
    found = 0
    for key, a in list(RAW.items()):
        if key in STORE:
            continue
        if pat.search(a["title"]) or (pat_n and pat_n.search(norm(a["title"]))):
            a["themes"] = themes_of(a["title"])
            STORE[key] = {"a": a, "c": c}
            RAW.pop(key, None)
            found += 1
    return found


BLOCK_BING = {"until": 0, "step": 900}


def fetch_local(c):
    """Cascade pour une commune : Google Actualités -> Bing Actualités -> flux déjà collectés (sans requête)."""
    q = '%s "%s"' % (CITY_TERMS, c["nom"])
    arts, via = [], "cache"
    if not blocked():
        arts = run_job((fetch_google, q))
        via = "google" if arts else via
    if not arts and time.time() >= BLOCK_BING["until"]:
        arts = run_job((fetch_bing, q))
        via = "bing" if arts else via
    ingest(arts, c)
    n = scan_raw(c)
    return via if (arts or not n) else "cache"


STORE_PATH = os.path.join(HERE, "store.json")


def save_store():
    try:
        with LOCK:
            blob = json.dumps(list(STORE.values()), ensure_ascii=False)
        tmp = STORE_PATH + ".tmp"
        open(tmp, "w", encoding="utf-8").write(blob)
        os.replace(tmp, STORE_PATH)
    except Exception as e:
        print("sauvegarde impossible:", e)


def load_store():
    try:
        cutoff = time.time() - 86400
        for v in json.load(open(STORE_PATH, encoding="utf-8")):
            if v["a"]["ts"] >= cutoff:
                STORE[norm(v["a"]["title"])[:80]] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print("lecture sauvegarde impossible:", e)



# ---------------------------------------------------------------------------
# Historique : base SQLite (articles du jour, positions des points, totaux journaliers)
# ---------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Paris")
except Exception:  # fuseau indisponible (ex. Windows sans tzdata) : repli sur l'heure d'été française
    TZ = timezone(timedelta(hours=2))
DB_PATH = os.path.join(HERE, "history.db")
DB_LOCK = threading.Lock()
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def day_of(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with DB_LOCK:
        c = db()
        try:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS articles(
                h TEXT NOT NULL, pid TEXT NOT NULL, day TEXT NOT NULL, ts REAL NOT NULL, title TEXT, src TEXT, link TEXT,
                nom TEXT, dep TEXT, lat REAL, lon REAL, pop INTEGER, themes TEXT, first_seen REAL,
                PRIMARY KEY (h, pid));
            CREATE INDEX IF NOT EXISTS idx_articles_day ON articles(day);
            CREATE TABLE IF NOT EXISTS daily_points(
                day TEXT NOT NULL, pid TEXT NOT NULL, nom TEXT, dep TEXT, lat REAL, lon REAL, pop INTEGER,
                n INTEGER, sources INTEGER, themes TEXT, PRIMARY KEY (day, pid));
            CREATE TABLE IF NOT EXISTS daily_totals(day TEXT PRIMARY KEY, articles INTEGER, places INTEGER, finalized_at REAL);
            CREATE TABLE IF NOT EXISTS rejected(
                h TEXT NOT NULL, pid TEXT NOT NULL, day TEXT, ts REAL, title TEXT, src TEXT, link TEXT, nom TEXT, dep TEXT,
                reason TEXT, score REAL, at REAL, PRIMARY KEY (h, pid));
            """)
            c.commit()
        finally:
            c.close()


def hist_record(items):
    """Enregistre (sans doublon) les articles courants, rangés par jour de publication (heure de Paris)."""
    now = time.time()
    rows = []
    for a, c in items:
        link = a.get("link") or ""
        h = a.get("h") or link_id(link)
        rows.append((h, "%s|%s" % (c["nom"], c.get("dep") or ""), day_of(a["ts"]), a["ts"], a.get("title", ""), a.get("src", ""), link,
                     c["nom"], c.get("dep"), c["lat"], c["lon"], c.get("pop") or 0, json.dumps(a.get("themes") or []), now))
    if not rows:
        return
    with DB_LOCK:
        conn = db()
        try:
            conn.executemany("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()
        finally:
            conn.close()


def reject_record(a, reason):
    """Garde une trace (réversible) d'un article écarté par le tri : motif « hs » ou « ancien »."""
    try:
        link = a.get("link") or ""
        row = (a.get("h") or link_id(link), "", day_of(a["ts"]), a["ts"], a.get("title", ""), a.get("src", ""), link, None, None,
               reason, round(HSX.hs_score(a.get("title", "")), 3), time.time())
        with DB_LOCK:
            conn = db()
            try:
                conn.execute("INSERT OR IGNORE INTO rejected VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print("tri : trace du rejet impossible", e)


def retro_record(a, c, day):
    """Compte rendu tardif d'un événement passé : rangé au jour de l'événement (la date de parution reste dans ts)."""
    link = a.get("link") or ""
    row = (a.get("h") or link_id(link), "%s|%s" % (c["nom"], c.get("dep") or ""), day, a["ts"], a.get("title", ""), a.get("src", ""), link,
           c["nom"], c.get("dep"), c["lat"], c["lon"], c.get("pop") or 0, json.dumps(themes_of(a.get("title", ""))), time.time())
    with DB_LOCK:
        conn = db()
        try:
            conn.execute("INSERT OR IGNORE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            conn.commit()
        finally:
            conn.close()


def finalize_day(day):
    """Fige les positions des points d'une journée (nombre d'articles, sources, thèmes par commune)."""
    with DB_LOCK:
        conn = db()
        try:
            rows = conn.execute("SELECT pid,nom,dep,lat,lon,pop,src,themes FROM articles WHERE day=?", (day,)).fetchall()
            places = {}
            for pid, nom, dep, lat, lon, pop, src, th in rows:
                p = places.setdefault(pid, {"nom": nom, "dep": dep, "lat": lat, "lon": lon, "pop": pop, "n": 0, "src": set(), "th": {}})
                p["n"] += 1
                p["src"].add(src)
                for t in json.loads(th or "[]"):
                    p["th"][t] = p["th"].get(t, 0) + 1
            conn.execute("DELETE FROM daily_points WHERE day=?", (day,))
            conn.executemany("INSERT INTO daily_points VALUES (?,?,?,?,?,?,?,?,?,?)",
                             [(day, pid, p["nom"], p["dep"], p["lat"], p["lon"], p["pop"], p["n"], len(p["src"]), json.dumps(p["th"]))
                              for pid, p in places.items()])
            conn.execute("INSERT OR REPLACE INTO daily_totals VALUES (?,?,?,?)", (day, len(rows), len(places), time.time()))
            conn.commit()
            return len(rows), len(places)
        finally:
            conn.close()


def finalize_missing():
    """Fige toutes les journées terminées qui ne l'ont pas été, et re-fige les deux dernières (articles tardifs)."""
    today = day_of(time.time())
    recent = {day_of(time.time() - 86400 * k) for k in (1, 2)}
    with DB_LOCK:
        conn = db()
        try:
            days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM articles WHERE day < ?", (today,))]
            done = {r[0] for r in conn.execute("SELECT day FROM daily_totals")}
        finally:
            conn.close()
    for d in sorted(days):
        if d not in done or d in recent:
            n, pl = finalize_day(d)
            print("historique : journée %s figée (%d articles, %d lieux)" % (d, n, pl))


def daily_loop():
    """Équivalent d'un cron à minuit (heure de Paris), dans l'application : pas de souci de fuseau serveur (UTC),
    et rattrapage des journées manquées après un redémarrage."""
    while True:
        try:
            finalize_missing()
        except Exception as e:
            print("historique : erreur", e)
        now = datetime.now(TZ)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        time.sleep(max(60, (nxt - now).total_seconds()))


def history_days():
    with DB_LOCK:
        conn = db()
        try:
            rows = conn.execute("SELECT day, COUNT(*), COUNT(DISTINCT pid) FROM articles GROUP BY day ORDER BY day DESC LIMIT 60").fetchall()
        finally:
            conn.close()
    return {"today": day_of(time.time()), "days": [{"day": d, "articles": n, "places": p} for d, n, p in reversed(rows)]}


def history_day(day):
    if not DAY_RE.match(day or ""):
        return {"error": "jour invalide"}
    with DB_LOCK:
        conn = db()
        try:
            rows = conn.execute("SELECT pid,nom,dep,lat,lon,pop,title,src,ts,themes,h FROM articles WHERE day=? ORDER BY ts DESC", (day,)).fetchall()
        finally:
            conn.close()
    places = {}
    for pid, nom, dep, lat, lon, pop, title, src, ts, th, h in rows:
        p = places.setdefault(pid, {"nom": nom, "dep": dep, "lat": lat, "lon": lon, "pop": pop, "n": 0, "last": ts, "articles": []})
        p["n"] += 1
        if len(p["articles"]) < 8:
            p["articles"].append({"title": title, "src": src, "ts": ts, "themes": json.loads(th or "[]"), "h": h})
    ev = sorted(places.values(), key=lambda e: (-e["n"], -e["last"]))
    return {"day": day, "total": len(rows), "events": ev}


def db_link(gid):
    try:
        with DB_LOCK:
            conn = db()
            try:
                r = conn.execute("SELECT link FROM articles WHERE h=? LIMIT 1", (gid,)).fetchone()
            finally:
                conn.close()
        return r[0] if r else None
    except Exception:
        return None


LINKMAP = {}  # id court -> URL de l'article (les liens ne sont plus envoyés dans la charge initiale)


def link_id(url):
    return base64.urlsafe_b64encode(hashlib.sha1(url.encode("utf-8")).digest())[:10].decode()


def slim_events(state):
    """Version allégée pour le navigateur : titres, sources, dates, thèmes et id de lien (≈ -90 % de poids)."""
    out = dict(state)
    out["events"] = [{**e, "articles": [{"title": a["title"], "src": a.get("src", ""), "ts": a["ts"], "themes": a.get("themes"), "h": a["h"]}
                                        for a in e["articles"] if a.get("h")]} for e in state["events"]]
    return out


_PURGED = [False]


def rebuild():
    cutoff = time.time() - 86400
    if not _PURGED[0]:  # une seule fois : le stock repris de store.json n'est pas passé par le tri
        _PURGED[0] = True
        with LOCK:
            for k in list(STORE):
                v = STORE[k]
                try:
                    if HSX.verdict(v["a"]["title"], v["a"]["ts"])[0] != "ok" or not KEYWORDS.search(NOISE.sub(" ", v["a"]["title"])):
                        del STORE[k]
                except Exception:
                    pass
    # Avis de Jev pour les articles du stock qui n'en ont pas encore (hors verrou ; plafonné par minute, le reste au cycle suivant)
    with LOCK:
        todo = [(k, v["a"]["title"]) for k, v in STORE.items() if v["a"].get("jev") is None and not HSX.has_evidence(v["a"]["title"])]
    bad = []
    for k, title in todo:
        g = jev_gate(title)
        if g is None:
            break
        if g == "hs":
            bad.append(k)
        with LOCK:
            if k in STORE:
                STORE[k]["a"]["jev"] = g
    if bad:
        with LOCK:
            for k in bad:
                STORE.pop(k, None)
        print("jev : %d article(s) écarté(s) comme hors sujet" % len(bad))
    for k in [k for k, a in RAW.items() if a["ts"] < cutoff]:
        RAW.pop(k, None)
    places = {}
    with LOCK:
        for k in [k for k, v in STORE.items() if v["a"]["ts"] < cutoff]:
            del STORE[k]
        LINKMAP.clear()
        for v in STORE.values():
            c = v["c"]
            a = v["a"]
            a["h"] = link_id(a["link"] or "")
            if a["link"]:
                LINKMAP[a["h"]] = a["link"]
            ad = announced_day(a["title"], a["ts"])
            a["avenir"] = ad.isoformat() if ad else ""
            if a["avenir"] > day_of(time.time()):
                continue  # événement annoncé pour plus tard : il est dans l'onglet « À venir », pas sur la carte « En direct »
            places.setdefault(c["nom"] + c["dep"], {**c, "articles": []})["articles"].append(a)
        events = []
        for p in places.values():
            p["articles"].sort(key=lambda a: -a["ts"])
            events.append({"nom": p["nom"], "dep": p["dep"], "lat": p["lat"], "lon": p["lon"], "pop": p["pop"],
                           "last": p["articles"][0]["ts"],
                           "sources": len({a["src"] for a in p["articles"]}),
                           "articles": p["articles"][:8], "n": len(p["articles"])})
        STATE.update(events=events, updated=time.time(), status="ok")
        snap = [(v["a"], v["c"]) for v in STORE.values()]
    save_store()
    try:
        hist_record(snap)
    except Exception as e:
        print("historique : enregistrement impossible", e)
    return events


def refresh():
    hits0 = GCAP["hits"]
    allj = jobs()
    feeds_j = [j for j in allj if j[0] is fetch_feed]
    other_j = [j for j in allj if j[0] is not fetch_feed]
    with ThreadPoolExecutor(max_workers=8) as ex_f, ThreadPoolExecutor(max_workers=2) as ex_o:
        fut_f = [ex_f.submit(run_job, j) for j in feeds_j]
        fut_o = [ex_o.submit(run_job, j) for j in other_j]
        results = [f.result() for f in fut_f] + [f.result() for f in fut_o]
    for arts in results:
        ingest(arts)
    now = time.time()
    for c in sorted(ALL, key=lambda c: -c["pop"])[:TOP_CITIES]:
        LAST_FETCH[(c["nom"], c["dep"])] = now
    if GOOGLE_ON and GCAP["hits"] == hits0 and time.time() >= BLOCK["until"] and GCAP["cap"] < GCAP["max"]:
        GCAP["cap"] = min(GCAP["max"], GCAP["cap"] + 4)
    ev = rebuild()
    print(f"{len(ev)} lieux, {sum(e['n'] for e in ev)} articles · Google : plafond {GCAP['cap']}/10 min" + (" (en pause)" if time.time() < BLOCK["until"] else ""))


def minpop_for_zoom(z):
    """Séquençage par taille de commune : plus on zoome, plus on descend vers les petites."""
    return None if z < 10 else 5000 if z == 10 else 2000 if z == 11 else 0


def area(s, w, n, e, z=9):
    """Interroge à la demande les communes visibles dont les données sont périmées."""
    if not AREA_LOCK.acquire(blocking=False):
        return {"busy": True}
    try:
        now = time.time()
        BUDGET[:] = [t for t in BUDGET if now - t < 600]
        mp = minpop_for_zoom(z)
        if mp is None:
            return {"queried": 0, "remaining": 0, "tooFar": True}
        inside = sorted((c for c in FULL if c["pop"] >= mp and s <= c["lat"] <= n and w <= c["lon"] <= e), key=lambda c: -c["pop"])
        stale = [c for c in inside if now - LAST_FETCH.get((c["nom"], c["dep"]), 0) > AREA_TTL]
        room = max(0, TOTAL_BUDGET - len(BUDGET))
        batch = stale[:min(AREA_MAX_PER_CALL, room)]
        if batch:
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(fetch_local, batch))
            for c in batch:
                LAST_FETCH[(c["nom"], c["dep"])] = now
            rebuild()
        return {"queried": len(batch), "remaining": len(stale) - len(batch), "limited": room == 0}
    finally:
        AREA_LOCK.release()


def search(q):
    q = norm(q)
    if len(q) < 2:
        return []
    hits = [c for c in FULL if q in c["n"]]
    hits.sort(key=lambda c: (not c["n"].startswith(q), -c["pop"]))
    return [{k: c[k] for k in ("nom", "dep", "lat", "lon", "pop", "code")} for c in hits[:8]]


def city(code):
    """Interroge à la demande une commune précise (recherche utilisateur), même petite."""
    c = next((c for c in FULL if c["code"] == code), None)
    if not c:
        return {"error": "inconnue"}
    info = {k: c[k] for k in ("nom", "dep", "lat", "lon", "pop", "code")}
    if not AREA_LOCK.acquire(timeout=10):
        return {"busy": True, "commune": info}
    try:
        now = time.time()
        BUDGET[:] = [t for t in BUDGET if now - t < 600]
        fresh = now - LAST_FETCH.get((c["nom"], c["dep"]), 0) <= AREA_TTL
        if not fresh and len(BUDGET) < TOTAL_BUDGET:
            via = fetch_local(c)
            LAST_FETCH[(c["nom"], c["dep"])] = now
            rebuild()
            return {"queried": 1, "via": via, "commune": info}
        return {"queried": 0, "limited": not fresh, "commune": info}
    finally:
        AREA_LOCK.release()


def loop():
    try:  # redémarrage récent : on reprend les données sauvegardées sans relancer 100+ requêtes
        age = time.time() - os.path.getmtime(STORE_PATH)
        if STORE and age < REFRESH:
            time.sleep(REFRESH - age)
    except OSError:
        pass
    while True:
        try:
            refresh()
        except Exception as e:
            STATE["status"] = f"erreur: {e}"
        time.sleep(REFRESH)



# ---------------------------------------------------------------------------
# Jeux de données « add-ons » : sources structurées, mises à jour 1 fois par jour
#  - féminicides : carte publique du collectif « Féminicides par compagnons ou ex » (export KML)
#  - décès liés à la police : liste publique de « Désarmons-les »
# ---------------------------------------------------------------------------
import math

YEAR = time.localtime().tm_year
MONTHS = {m: i + 1 for i, m in enumerate("janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split())}
MONTHS_RE = "|".join(MONTHS)
DATASET_TTL = 23 * 3600
DATASETS = {
    "feminicides": {"title": "Féminicides conjugaux", "path": os.path.join(HERE, "fem.json"),
                    "source": "Collectif Féminicides par compagnons ou ex", "url": "https://www.feminicides.fr/",
                    "note": "Décompte a minima établi par le collectif à partir de la presse ; il se limite aux féminicides conjugaux. Les noms sont retirés et la position est ramenée au centre de la commune."},
    "police": {"title": "Décès liés à la police", "path": os.path.join(HERE, "police.json"),
               "source": "Désarmons-les", "url": "https://desarmons.net/listes-des-victimes/personnes-tuees-par-les-forces-de-lordre/",
               "note": "Décès survenus lors ou à la suite d'interventions des forces de l'ordre, recensés par un collectif militant. Il ne s'agit pas de toutes les violences policières."},
    "travail": {"title": "Accidents du travail mortels", "path": os.path.join(HERE, "travail.json"),
                "source": "Accident du travail : silence, des ouvriers meurent", "url": "https://bsky.app/profile/accidentdutravail.bsky.social",
                "note": "Décès au travail recensés à partir de la presse par le compte « Accident du travail : silence, des ouvriers meurent » (Bluesky), avec l'accord de son auteur. "
                        "Décompte a minima : ne figurent que les cas publiés par le compte, sans les maladies professionnelles. Les noms sont retirés, la position est ramenée au centre de la commune "
                        "et la date est celle de la publication du compte, pas forcément celle du décès."},
    "ps": {"title": "Plans sociaux, fermetures et procédures d'entreprises", "path": os.path.join(HERE, "ps.json"),
           "source": "Plans sociaux et faillites en France", "url": "https://oraclemarin.fr/plans-sociaux/",
           "note": "Annonces de plans sociaux, fermetures de sites et procédures collectives d'entreprises de 50 salariés et plus, sur 12 mois (fichier public de oraclemarin.fr/plans-sociaux)."},
    "agenda": {"title": "Mobilisations à venir", "path": os.path.join(HERE, "agenda.json"), "ttl": 3600,
               "source": "Démosphère, communiqués syndicaux et presse locale", "url": "https://demosphere.net/",
               "note": "Manifestations, rassemblements et grèves annoncés dans les agendas Démosphère, appels nationaux des syndicats (CGT, FSU, Solidaires, SUD, FO, Union étudiante…) et annonces relevées dans les titres de la presse locale, triés automatiquement d'après leur titre "
                       "(ateliers, projections, débats et concerts écartés). Annonces militantes : vérifiez sur la fiche d'origine, un rendez-vous peut être déplacé ou annulé. "
                       "La couverture dépend des agendas locaux existants (Démosphère Paris a fermé : en Île-de-France, seules la presse et les syndicats alimentent cet onglet)."},
}
DS = {k: {"items": [], "updated": None} for k in DATASETS}
FEM_MAP = "1X-eWA3Es57ts50GRiKbKXSYp9FKcmnM"  # carte 2026 liée depuis feminicides.fr
DEPS = {}


def http_text(url, timeout=60):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode("utf-8", "replace")


def nearest_commune(lat, lon, max_km=25):
    best, bd = None, 1e9
    k = math.cos(math.radians(lat))
    for c in FULL:
        d = (c["lat"] - lat) ** 2 + ((c["lon"] - lon) * k) ** 2
        if d < bd:
            best, bd = c, d
    return best if best and bd ** 0.5 * 111 <= max_km else None


def parse_date(text, year=None):
    m = re.search(r"\b(\d{1,2})(?:er)? (%s)\b" % MONTHS_RE, text, re.I)
    if not m:
        return None
    try:
        return time.mktime((year or YEAR, MONTHS[m.group(2).lower()], int(m.group(1)), 12, 0, 0, 0, 0, -1))
    except Exception:
        return None


def mask_names(text):
    """Retire les prénoms/noms des victimes : on ne garde que l'âge (« une femme (63 ans) »)."""
    return re.sub(r"(?<=, )[A-ZÀ-Ý][\wÀ-ÿ'’-]+(?: [A-ZÀ-Ý][\wÀ-ÿ'’-]+){0,3} (\(\d+ ans\))", r"une femme \1", text)


def fetch_feminicides():
    if not DEPS:
        for d in json.loads(http_text("https://geo.api.gouv.fr/departements?fields=nom,code")):
            DEPS[norm(d["nom"])] = d["code"]
    kml = http_text("https://www.google.com/maps/d/kml?mid=%s&forcekml=1" % FEM_MAP)
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    items = []
    for p in ET.fromstring(kml.encode("utf-8")).iter("{http://www.opengis.net/kml/2.2}Placemark"):
        name = (p.findtext("k:name", namespaces=ns) or "").strip()
        if not re.match(r"\d+\s*-", name):
            continue  # on ignore la fiche « STATS »
        coords = (p.findtext(".//k:coordinates", namespaces=ns) or "").strip().split(",")
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        desc = html.unescape(re.sub(r"<[^>]+>", " ", p.findtext("k:description", namespaces=ns) or ""))
        m = re.search(r"https://www\.feminicides\.fr/\S+", desc)
        link = m.group(0) if m else DATASETS["feminicides"]["url"]
        text = re.split(r"➡|Lire la publication", desc)[0]
        text = re.sub(r"#\w+", "", text)
        text = mask_names(re.sub(r"\s+", " ", text)).strip()
        ts = parse_date(text)
        if not ts:
            continue
        c = locate_police(text[:300])  # commune citée dans le texte (plus fiable que le point)
        if not c or (c["lat"] - lat) ** 2 + ((c["lon"] - lon) * 0.7) ** 2 > 0.35 ** 2:
            c = nearest_commune(lat, lon)  # sinon, commune la plus proche : position ramenée au centre de la commune
        items.append({"ts": ts, "lat": c["lat"] if c else round(lat, 2), "lon": c["lon"] if c else round(lon, 2),
                      "place": c["nom"] if c else "", "dep": c["dep"] if c else "", "text": text[:420], "url": link})
    return items


def locate_police(text):
    """Trouve « Commune (Département) » dans le texte ; repli sur la détection de communes par majuscules."""
    for m in re.finditer(r"\(([^()]{2,40})\)", text):
        inner = norm(m.group(1))
        dm = re.match(r"(\d{2,3}|2[ab])\s*[-–]", inner) or re.match(r"(\d{2,3})$", inner)
        code = DEPS.get(inner) or (dm.group(1).upper() if dm else None) or ("75" if re.match(r"\d+(?:e|er|eme)\b", inner) else None)
        if not code:
            continue
        toks = re.findall(r"[\wÀ-ÿ-]+", text[:m.start()])[-6:]
        for n in range(min(5, len(toks)), 0, -1):
            cand = norm(" ".join(toks[-n:]))
            hit = next((c for c in FULL if c["dep"] == code and c["n"] == cand), None)
            if hit:
                return hit
        # variante d'orthographe (« Villiers-en-Arthes » pour Villiers-en-Arthies, « Barœul ») : commune la plus proche du même département
        import difflib
        noms = [c for c in FULL if c["dep"] == code]
        for n in range(min(4, len(toks)), 0, -1):
            proche = difflib.get_close_matches(norm(" ".join(toks[-n:])), [c["n"] for c in noms], n=1, cutoff=0.88)
            if proche:
                return next(c for c in noms if c["n"] == proche[0])
        if code == "75":
            return next((c for c in FULL if c["code"] == "75056"), None)
    return geocode(text)


def fetch_police():
    if not DEPS:
        for d in json.loads(http_text("https://geo.api.gouv.fr/departements?fields=nom,code")):
            DEPS[norm(d["nom"])] = d["code"]
    page = http_text(DATASETS["police"]["url"])
    i = page.find(">%d<" % YEAR)
    section = page[i:] if i >= 0 else page
    nxt = re.search(r">%d<" % (YEAR - 1), section)
    if nxt:
        section = section[:nxt.start()]
    items = []
    for li in re.findall(r"<li[^>]*>(.*?)</li>", section, re.S):
        link = re.search(r'href="([^"]+)"', li)
        t = html.unescape(re.sub(r"<[^>]+>", "", li))
        t = re.sub(r"\s+", " ", t).strip()
        if re.match(r"Le même jour\b", t) and items and re.search(r"décéd|mort|tué", t[:160]):    # second décès du même événement, ligne à part sur la page
            items[-1]["victimes"] = items[-1].get("victimes", 1) + 1
            continue
        m = re.match(r"Le (\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\s*,\s*(.*)", t)      # « Le 01/06/26 » ou « Le 3/07/2026 » existent aussi : ne retirer aucun cas recensé
        if not m or (int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)) != YEAR:
            continue
        rest = m.group(4)
        a = re.search(r"(?:environ |entre )?\d{1,2} ans", rest)
        after = rest[a.end():] if a else rest.split(",", 1)[-1]
        desc = after.lstrip(" ,")
        desc = re.sub(r"^(?:dite?|alias|surnomm[ée]e?)\s+[^,]{1,40},\s*", "", desc)  # surnom resté après le retrait du nom
        loc = locate_police(desc[:360])
        extra = re.search(r"\b(?:et|avec) (?:ses|leurs?) (deux|trois|quatre|cinq) (?:filles|fils|enfants)", rest)      # « Angéline X. et ses deux filles » : plusieurs victimes dans une ligne
        nv = 1 + ({"deux": 2, "trois": 3, "quatre": 4, "cinq": 5}[extra.group(1)] if extra else 0)
        items.append({"victimes": nv, "ts": time.mktime((YEAR, int(m.group(2)), int(m.group(1)), 12, 0, 0, 0, 0, -1)),
                      "lat": loc["lat"] if loc else None, "lon": loc["lon"] if loc else None,
                      "place": loc["nom"] if loc else "", "dep": loc["dep"] if loc else "",
                      "age": "" if nv > 1 else (a.group(0) if a else ""), "text": desc[:420],
                      "url": link.group(1) if link else DATASETS["police"]["url"]})
    return items


BSKY_ACTOR = "accidentdutravail.bsky.social"
TRAVAIL_RX = re.compile(r"(\d+)\s*morts?\s+recens\w*\s+en\s+(\d{4})\s*(.+?)\s*\(([0-9]{2,3}|2A|2B)\)\s*:\s*(.+)", re.S | re.I)
TRAVAIL_RX_NODEP = re.compile(r"(\d+)\s*morts?\s+recens\w*\s+en\s+(\d{4})\s*([^:()]{2,60}?)\s*:\s*(.+)", re.S | re.I)
TRAVAIL_NAME = re.compile(r"(?<![\wÀ-ÿ])[A-ZÀ-Ý][\wÀ-ÿ'’-]+(?: [A-ZÀ-Ý][\wÀ-ÿ'’-]+){1,3}, (?=[^,.;:()]{0,70}\d{1,3} ans)")
TRAVAIL_NAME_TITLE = re.compile(r"\b(capitaines?|ma[îi]tres? d['’]équipage|seconds?|patrons?|chefs?|m[ée]caniciens?|matelots?|ouvriers?|salari[ée]e?s?|conducteurs?|g[ée]rants?|directeurs?|dirigeants?) "
                                r"[A-ZÀ-Ý][\wÀ-ÿ'’-]+(?: [A-ZÀ-Ý][\wÀ-ÿ'’-]+){1,3}")      # « le capitaine Prénom Nom »
TRAVAIL_NAME_START = re.compile(r"^[A-ZÀ-Ý][\wÀ-ÿ'’-]+(?: [A-ZÀ-Ý][\wÀ-ÿ'’-]+){0,3}, ")      # « Prénom Nom, directeur de… » en début de texte


_DEP_POOL = {}


def _dep_pool(dep):
    if dep not in _DEP_POOL:
        _DEP_POOL[dep] = [c for c in FULL if c["dep"] in (("2A", "2B") if dep == "20" else (dep,))]
    return _DEP_POOL[dep]


def locate_travail(ville, dep):
    """Commune « ville » du département « dep » (ou unique en France si dep inconnu) ; None si introuvable."""
    import difflib
    first = re.split(r"\s*[,/]\s*|\s+et\s+", ville)[0]
    names = [norm(first), norm(re.sub(r"^(?:le|la|les|l)\s+", "", norm(first)))]
    pool = _dep_pool(dep) if dep else FULL
    for n in names:
        hits = [c for c in pool if c["n"] == n]
        if len(hits) == 1 or (hits and dep):
            return max(hits, key=lambda c: c["pop"])
    if not dep:
        return None
    for n in names:                      # « Châtelaillon » pour Châtelaillon-Plage, « Pouzauges » pour Pouzauges (nouvelle commune)
        hits = [c for c in pool if c["n"].startswith(n + " ")]
        if hits:
            return max(hits, key=lambda c: c["pop"])
    for n in names:                      # « Valence-d'Agen » devenue Valence : on retire les derniers mots
        toks = n.split()
        for k in range(len(toks) - 1, 0, -1):
            hits = [c for c in pool if c["n"] == " ".join(toks[:k])]
            if hits and len(" ".join(toks[:k])) >= 5:
                return max(hits, key=lambda c: c["pop"])
    for n in names:
        close = difflib.get_close_matches(n, [c["n"] for c in pool], n=1, cutoff=0.86)
        if close:
            return next(c for c in pool if c["n"] == close[0])
    return None


def fetch_travail():
    import calendar
    posts, cur = [], None
    for _ in range(30):
        q = {"actor": BSKY_ACTOR, "limit": 100, "filter": "posts_no_replies"}
        if cur:
            q["cursor"] = cur
        d = json.loads(http_text("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed?" + urllib.parse.urlencode(q), 30))
        for f in d.get("feed", []):
            if "reason" in f:      # republication d'un autre compte
                continue
            p = f["post"]
            posts.append({"uri": p["uri"], "date": p["record"]["createdAt"], "text": p["record"].get("text", ""),
                          "link": ((p.get("embed") or {}).get("external") or {}).get("uri", "")})
        cur = d.get("cursor")
        if not cur or not d.get("feed") or posts[-1]["date"][:4] < str(YEAR):
            break
    best = {}
    for p in posts:
        m = TRAVAIL_RX.search(p["text"])
        if m:
            n, yr, ville, dep, desc = int(m.group(1)), m.group(2), re.sub(r"\s+", " ", m.group(3)).strip(), m.group(4), m.group(5)
        else:
            m = TRAVAIL_RX_NODEP.search(p["text"])
            if not m:
                continue
            n, yr, ville, dep, desc = int(m.group(1)), m.group(2), re.sub(r"\s+", " ", m.group(3)).strip(), None, m.group(4)
        if yr != str(YEAR):
            continue
        desc = re.sub(r"@\S+", "", desc)
        for _ in range(2):             # lien d'article tronqué en fin de publication
            desc = re.sub(r"\s*\S*(?:/|https?:|www\.)\S*\s*$", "", desc.strip())
        desc = re.sub(r"\s+", " ", TRAVAIL_NAME_START.sub("", TRAVAIL_NAME.sub("", TRAVAIL_NAME_TITLE.sub(r"\1", desc)))).strip()    # retire « Prénom Nom, » (devant l'âge ou en début de texte)
        if len(desc) < 20:
            continue
        c = locate_travail(ville, dep)
        link = re.sub(r"#.*$", "", p["link"]) if p["link"].startswith("http") else ""
        post = "https://bsky.app/profile/%s/post/%s" % (BSKY_ACTOR, p["uri"].rsplit("/", 1)[1])
        ts = calendar.timegm(time.strptime(p["date"][:19], "%Y-%m-%dT%H:%M:%S"))
        if n in best:                  # même numéro publié deux fois : on garde la publication la plus récente (rencontrée en premier)
            continue
        best[n] = {"n": n, "ts": ts, "lat": c["lat"] if c else None, "lon": c["lon"] if c else None,
                   "place": c["nom"] if c else ville, "dep": c["dep"] if c else (dep or ""), "text": desc[:420],
                   "url": link or post, "post": post}
    return list(best.values())


# Agendas Démosphère francophones de France (bxl, liege, ffm, wuerzburg, saopaulo écartés)
DEMOSPHERE = ["05", "ariege", "aveyron", "berry", "carcassonne", "dunkerque", "gard", "gironde", "lille", "lot", "nice", "paris",
              "poitiers", "pyrenees", "rennes", "sarthe", "strasbourgfurieuse", "tarn", "toulouse"]
# Les catégories varient d'un agenda à l'autre (« Lutter-Résister » couvre surtout permanences et projections) : le tri se fait sur le titre.
AG_POS = re.compile(r"\bmanif(?:s|estations?)?\b|\brassemblements?\b|\bmarches?\b|\bgr[eè]ves?\b|\bgr[eé]vistes?\b|\bpiquets?\b|"
                    r"\bblocages?\b|\bblocus\b|\bbloquons\b|\bmobilisations?\b|\bcort[eè]ges?\b|\bsit-in\b|\bdie-in\b|cha[iî]ne humaine|"
                    r"cercles? de silence|\bprotestations?\b|\boccupation\b|^free palestine|\bstop g[ée]nocide|r[ée]gularisez|\bnon (?:au|aux|à la|à l')\b|journ[ée]e (?:nationale )?(?:de gr[eè]ve|d'action|de mobilisation)|interprofessionnelle|"
                    r"\bv[ée]lorution\b|\bkidical mass\b|\bpride\b|marche des fiert[ée]s|\b(?:ni|pas de) congr[eè]s\b|contre l'inauguration", re.I)
AG_NEG = re.compile(r"\bprojection|\bfilm\b|\bcin[ée]|\bconcert|\batelier|\bd[ée]bat\b|\bconf[ée]rence|\br[ée]union\b|\bassembl[ée]e g[ée]n[ée]rale|"
                    r"\bformation\b|\bpermanence|\brepas\b|\bcantine|\bexpo(?:sition)?\b|\blibrairie|\blecture|\bspectacle|\bth[ée][aâ]tre|"
                    r"\bap[ée]ro|\bsoir[ée]e\b|(?<!d[ée]-)\bf[eê]te\b|\bbal\b|\bjeux?\b|\bmarch[ée]s\b|\bmarch[é]\b|\bbrocante|\bvide-grenier|\bsalon\b|\bfestival|"
                    r"\bcaf[ée][- ]|\bcours\b|\bstage\b|\bpr[ée]sentation\b|\bdiscussion\b|\brencontre\b|\bpodcast|\bradio\b|\b[ée]mission|\bannul[ée]", re.I)
AG_CAT = re.compile(r"^manif", re.I)  # « Manif/Action », « Manifestation », « Manif' » : seule catégorie assez nette pour suffire
AG_DAY = re.compile(r"^\s*(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+\d{1,2}(?:\s+\w+\s+\d{4})?\s+-\s+", re.I)


def agenda_keep(title, cats):
    if AG_NEG.search(title):
        return False
    return bool(AG_POS.search(title) or any(AG_CAT.search(c) for c in cats))


def fetch_agenda():
    now = time.time()
    seen, items = [], []
    for inst in DEMOSPHERE:
        try:
            xml = http_text("https://%s.demosphere.net/events.xml?limit=300" % inst, timeout=30)
        except Exception as e:
            print("agenda", inst, "erreur:", e)
            continue
        for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
            g = lambda p: html.unescape((re.search(p, it, re.S) or [None, ""])[1]).strip()
            where = g(r"<dcterms:spatial>(.*?)</dcterms:spatial>")
            title = AG_DAY.sub("", g(r"<title>(.*?)</title>"))
            if where and title.startswith(where + " - "):
                title = title[len(where) + 3:]
            title = re.sub(r"\s+", " ", title).strip(" -")
            cats = [html.unescape(c) for c in re.findall(r"<category>(.*?)</category>", it)]
            start = g(r"start=([^;]+);")
            try:
                ts = datetime.fromisoformat(start).timestamp()
            except Exception:
                continue
            if not (now - 6 * 3600 <= ts <= now + 60 * 86400) or not agenda_keep(title, cats):
                continue
            nat = norm(where.lstrip("#")) in ("france", "national", "partout", "partout en france")
            c, pt = None, g(r"<georss:point>(.*?)</georss:point>").split()
            if not nat and len(pt) == 2:
                try:
                    c = nearest_commune(float(pt[0]), float(pt[1]))
                except ValueError:
                    pass
            if not nat and not c and where:
                w = re.sub(r"\s*\d{5}$", "", where.lstrip("#"))
                c = COMMUNES.get(norm(w)) or geocode(w)
            if not nat and not c:
                continue  # hors de France ou lieu introuvable
            # même rendez-vous relayé par plusieurs agendas, parfois sous un titre ou une commune voisine différents
            words = {w for w in norm(title).split() if len(w) > 3}
            pos = (c["lat"], c["lon"]) if c else (0.0, 0.0)  # même jour, à moins de ~15 km (communes voisines), titres proches
            if any(day == start[:10] and abs(p[0] - pos[0]) < 0.14 and abs(p[1] - pos[1]) < 0.2
                   and len(words & w) >= 0.6 * min(len(words), len(w)) for day, p, w in seen):
                continue
            seen.append((start[:10], pos, words))
            items.append({"ts": ts, "lat": c["lat"] if c else None, "lon": c["lon"] if c else None,
                          "place": c["nom"] if c else "", "dep": c["dep"] if c else "", "nat": nat,
                          "org": "Démosphère" if nat else "", "text": title[:300], "url": g(r"<link>(.*?)</link>")})
        time.sleep(1)
    try:
        items += press_calls(now)
    except Exception as e:
        print("appels presse : erreur", e)
    return items + fetch_appels(now)


# Appels nationaux des organisations syndicales (flux RSS) : on garde les annonces récentes qui donnent une date à venir.
# FIDL, MNL, La Voix lycéenne : pas de flux (réseaux sociaux seulement) ; leurs appels passent par les communiqués intersyndicaux.
APPEL_FEEDS = {"CGT": "https://www.cgt.fr/rss.xml", "FSU": "https://fsu.fr/feed/", "Solidaires": "https://www.solidaires.org/feed/",
               "SUD Éducation": "https://www.sudeducation.org/feed/", "SNESUP-FSU": "https://www.snesup.fr/rss.xml",
               "L'Union étudiante": "https://union-etudiante.fr/feed/", "FO": "https://www.force-ouvriere.fr/spip.php?page=backend",
               "SUD-Rail": "https://www.sudrail.fr/spip.php?page=backend"}
APPEL_RE = re.compile(r"gr[eè]ve|mobilis|manif|rassembl|journ[ée]e d'action|dans la rue|appel", re.I)
APPEL_DATE = re.compile(r"\b(\d{1,2})(?:er)?\s+(%s)\b" % MONTHS_RE, re.I)


def appel_date(text, now):
    """Première date à venir citée (« le 13 octobre », « jeudi 5 novembre ») ; l'année est déduite."""
    today = datetime.fromtimestamp(now).date()
    for m in APPEL_DATE.finditer(text):
        mo, d = MONTHS[m.group(2).lower()], int(m.group(1))
        for y in (today.year, today.year + 1):
            try:
                dt = datetime(y, mo, d, 12)
            except ValueError:
                break
            if today <= dt.date() <= today + timedelta(days=60):
                return dt.timestamp()
    return None


def fetch_appels(now):
    out = []
    for org, url in APPEL_FEEDS.items():
        try:
            xml = http_text(url, timeout=30)
        except Exception as e:
            print("appels", org, "erreur:", e)
            continue
        for it in re.findall(r"<item>(.*?)</item>", xml, re.S)[:20]:
            g = lambda p: html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", " ", html.unescape((re.search(p, it, re.S) or [None, ""])[1]))).strip()
            title, desc = re.sub(r"\s+", " ", g(r"<title>(.*?)</title>")), re.sub(r"\s+", " ", g(r"<description>(.*?)</description>"))[:400]
            pub = feed_ts(g(r"<pubDate>(.*?)</pubDate>") or g(r"<dc:date>(.*?)</dc:date>"))
            if pub and pub < now - 30 * 86400:
                continue  # annonce trop ancienne : la date citée viserait une autre année
            ts = appel_date(title, now) or (appel_date(desc, now) if APPEL_RE.search(desc) else None)
            if not ts or not APPEL_RE.search(title + " " + desc):
                continue
            if any(o["org"] == org and o["ts"] == ts for o in out):
                continue  # une annonce par organisation et par journée suffit
            out.append({"ts": ts, "lat": None, "lon": None, "place": "", "dep": "", "nat": True, "org": org,
                        "text": title[:300], "url": g(r"<link>(.*?)</link>")})
    return out


# Appels relevés dans la presse : titres d'articles déjà géolocalisés (history.db) qui annoncent une mobilisation datée.
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
PR_FUT = re.compile(r"\bappel(?:le|lent|ent)?s?\b|\bappell|pr[ée]avis|pr[ée]vue?s?\b|\bannonc|\bva\b|\bvont\b|\borganis|se pr[ée]pare|\bmenace|"
                    r"\bcomptent\b|\bveulent\b|\bpr[ée]voi|\w{3,}eront\b|\bavant (?:le|la)\b|\bà partir d|\bd[èe]s (?:le|ce)\b|"
                    r"[àa] quoi s'attendre|\bce (?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b|\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) prochain\b|"
                    r"\bdemain\b|\bce week-?end\b|\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche) \d{1,2}(?:er)? (?:janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|octobre|novembre|d[ée]cembre)\b", re.I)
PR_PAST = re.compile(r"\bont\b|\bavaient\b|\b[ée]taient\b|\bhier\b|\bbilan\b|\bs'est\b|\bse sont\b|\bavait\b|\ba (?:été|réuni|rassemblé|manifesté|défilé|mobilisé)\b|"
                     r"\bce matin\b|\baujourd'hui\b|\ben ce moment\b|\ben images\b|\bvid[ée]o\b|\bphotos?\b|\ben direct\b|\blive\b|"
                     r"jusqu'(?:au|à)|\brouvr|\bresteront\b|\binterdi|v[ée]hicules anciens|\bcommentez\b|\bRT:", re.I)
PR_NEG = re.compile(r"voitures|\bmotos? am|v[ée]hicules|auto-moto|collection|nettoy|centre aquatique|\bouvre\b|festif et gourmand|concert|festival|"
                    r"\bsalon\b|brocante|exposition|montr[ée]al|qu[ée]bec|belgique|suisse", re.I)
PR_EVENT = re.compile(r"manif|rassembl|gr[eè]ve|mobilis|blocage|blocus|bloquer|marche|cort[eè]ge|piquet|journ[ée]e d'action", re.I)
PR_DATE = re.compile(r"\b(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?(\d{1,2})(?:er)?\s+(%s)\b|\b(demain)\b|"
                     r"\b(?:(ce|cette|le|du|prochain|dès)\s+)?(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)(?:\s+(prochain))?\b" % MONTHS_RE, re.I)


def press_date(title, pub):
    """Date annoncée dans un titre, relative au jour de publication : (date, date explicite ?)."""
    pd = datetime.fromtimestamp(pub).date()
    for m in PR_DATE.finditer(title):
        if m.group(1):
            mo, dd = MONTHS[m.group(2).lower()], int(m.group(1))
            for y in (pd.year, pd.year + 1):
                try:
                    dt = datetime(y, mo, dd).date()
                except ValueError:
                    break
                if dt >= pd:
                    return dt, True
            continue
        if m.group(3):
            return pd + timedelta(days=1), False
        if m.group(5):
            delta = (JOURS.index(m.group(5).lower()) - pd.weekday()) % 7
            if delta == 0 and (m.group(6) or m.group(4) == "prochain"):
                delta = 7
            return pd + timedelta(days=delta), False
    return None, False


def announced_day(title, ts):
    """Jour d'un événement ANNONCÉ par le titre (strictement après sa publication), ou None. Mêmes règles que press_calls :
    un tel article va dans l'onglet « À venir » et ne doit pas apparaître sur la carte « En direct » avant le jour J."""
    t = (title or "").replace("’", "'")
    if not KEYWORDS.search(NOISE.sub(" ", t)) or not PR_EVENT.search(t) or PR_PAST.search(t) or PR_NEG.search(t):
        return None
    day, explicit = press_date(t, ts)
    if not day or day <= datetime.fromtimestamp(ts).date() or day > datetime.fromtimestamp(ts).date() + timedelta(days=60):
        return None
    if not PR_FUT.search(t) and not explicit:
        return None
    return day



LIST_TITLE = re.compile(r"o[uù] (?:sont|a lieu|manifester|se rassembler|se tiendront)|liste des|rendez-vous|tous les (?:points|rassemblements)|programme|carte des|"
                        r"(?:\d+|plusieurs|dizaines|nombreux|nombreuses|principaux|principales) (?:rassemblements|manifestations|points de rassemblement|actions|rendez-vous|mobilisations|lieux)|"
                        r"actions (?:annonc|pr[ée]vue)", re.I)
BODY_LINE = re.compile(r"^\W*([A-ZÀ-Ý][\wÀ-ÿ'’ -]{1,38}?)\s*(?:\([\w ]{2,22}\))?\s*(?::|\s[–—-]\s)\s*(.{4,220})$")
BODY_TIME = re.compile(r"\b\d{1,2}\s*(?:h|:)\s*\d{0,2}|\b(?:place|rond-point|mairie|pr[ée]fecture|parvis|gare|esplanade|avenue|boulevard|rue|carrefour|devant)\b", re.I)
BODY_CACHE = {}  # url -> (ts, lignes)


def body_lines(url):
    hit = BODY_CACHE.get(url)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    try:
        t = http_text(url, timeout=20)
    except Exception:
        BODY_CACHE[url] = (time.time(), [])
        return []
    t = re.sub(r"(?is)<(script|style|nav|footer|header|aside)\b.*?</\1>", " ", t)
    t = re.sub(r"(?i)<(?:br|/p|/li|/h\d|/div|/tr)[^>]*>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t)).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", x).strip() for x in t.split("\n")]
    lines = [x for x in lines if 6 <= len(x) <= 260]
    BODY_CACHE[url] = (time.time(), lines)
    return lines


def harvest_lists(rows, today, limit=30):
    """Articles-listes (« où sont prévus les rassemblements ») : une ligne « Commune : lieu à 14h » = un rendez-vous."""
    out, done = [], 0
    for ts, title, src, link, nom, dep, lat, lon in rows:
        title = (title or "").replace("’", "'")
        if not link or "news.google.com" in link or not LIST_TITLE.search(title) or not PR_EVENT.search(title) or PR_NEG.search(title):
            continue
        day, _ = press_date(title, ts)
        if not day or day < today or day > today + timedelta(days=60):
            continue
        if link not in BODY_CACHE:
            if done >= limit:
                continue
            done += 1
        for line in body_lines(link):
            m = BODY_LINE.match(line)
            if not m or not BODY_TIME.search(m.group(2)):
                continue
            name = norm(m.group(1))
            c = COMMUNES.get(name)
            if not c:
                cands = [x for x in FULL if x["n"] == name and (not dep or x["dep"] == dep)]
                c = max(cands, key=lambda x: x["pop"]) if cands and (dep or len(cands) == 1) else None
            if not c:
                continue
            out.append({"ts": datetime(day.year, day.month, day.day, 12).timestamp(), "day": True, "lat": c["lat"], "lon": c["lon"],
                        "place": c["nom"], "dep": c.get("dep") or "", "nat": False, "org": "", "press": src or "presse",
                        "text": (m.group(1) + " : " + m.group(2))[:300], "url": link, "multi": True})
    return out


def press_calls(now):
    with DB_LOCK:
        conn = db()
        try:
            rows = conn.execute("SELECT ts, title, src, link, nom, dep, lat, lon FROM articles WHERE ts >= ?", (now - 21 * 86400,)).fetchall()
        finally:
            conn.close()
    today = datetime.fromtimestamp(now).date()
    best = {}
    for ts, title, src, link, nom, dep, lat, lon in rows:
        title = (title or "").replace("’", "'")
        if not KEYWORDS.search(NOISE.sub(" ", title)):
            continue  # « appel à manifestation d'intérêt », « rassembler » (unir) : pas une annonce de mobilisation
        if not PR_EVENT.search(title) or PR_PAST.search(title) or PR_NEG.search(title):
            continue
        day, explicit = press_date(title, ts)
        if not day or day < today or day > today + timedelta(days=60):
            continue
        if day == datetime.fromtimestamp(ts).date() and not re.search(r"appel|pr[ée]avis|pr[ée]vue?s?\b", title, re.I):
            continue  # même jour que la publication : souvent un compte rendu, on n'accepte qu'une annonce explicite
        # sans verbe d'annonce, seule une date explicite postérieure à la publication vaut annonce
        if not PR_FUT.search(title) and not (explicit and day > datetime.fromtimestamp(ts).date()):
            continue
        nat = nom == "L'Union" and "union étudiante" in title.lower()  # « L'Union étudiante » prise pour la commune
        multi = [] if nat else [m for m in list_communes(title) if (m["nom"], m["dep"]) != (nom, dep)]
        for m in multi:  # titre qui liste plusieurs communes : un rendez-vous par commune citée
            k2 = (day, m["nom"] + (m["dep"] or ""))
            sc2 = (0, ts)
            if k2 not in best or sc2 > best[k2][0]:
                best[k2] = (sc2, {"ts": datetime(day.year, day.month, day.day, 12).timestamp(), "day": True, "lat": m["lat"], "lon": m["lon"],
                                  "place": m["nom"], "dep": m["dep"] or "", "nat": False, "org": "", "press": src or "presse",
                                  "text": title[:300], "url": link, "multi": True})
        key = (day, "FR" if nat else nom + (dep or ""))
        score = (1 if re.search(r"appel|pr[ée]avis", title, re.I) else 0, ts)  # on préfère l'article qui parle d'appel, puis le plus récent
        if key not in best or score > best[key][0]:
            best[key] = (score, {"ts": datetime(day.year, day.month, day.day, 12).timestamp(), "day": True,
                                 "lat": None if nat else lat, "lon": None if nat else lon, "place": "" if nat else nom,
                                 "dep": "" if nat else dep, "nat": nat, "org": "presse" if nat else "", "press": src or "presse",
                                 "text": title[:300], "url": link})
    for it in harvest_lists(rows, today):
        k3 = (datetime.fromtimestamp(it["ts"]).date(), it["place"] + (it["dep"] or ""))
        if k3 not in best:
            best[k3] = ((0, 0), it)
    return [v[1] for v in best.values()]


def refresh_dataset(name):
    ds = DATASETS[name]
    try:
        items = {"feminicides": fetch_feminicides, "police": fetch_police, "travail": fetch_travail, "ps": fetch_ps, "agenda": fetch_agenda}[name]()
    except Exception as e:
        print("add-on", name, "erreur:", e)
        return False
    items.sort(key=lambda i: i["ts"] if name == "agenda" else -i["ts"])  # agenda : du plus proche au plus lointain
    DS[name] = {"items": items, "updated": time.time()}
    try:
        tmp = ds["path"] + ".tmp"
        open(tmp, "w", encoding="utf-8").write(json.dumps(DS[name], ensure_ascii=False))
        os.replace(tmp, ds["path"])
    except Exception as e:
        print("sauvegarde add-on impossible:", e)
    print("add-on %s: %d entrées, %d localisées" % (name, len(items), sum(1 for i in items if i.get("lat") is not None)))
    return True


def datasets_loop():
    for name, ds in DATASETS.items():
        try:
            DS[name] = json.load(open(ds["path"], encoding="utf-8"))
        except Exception:
            pass
    while True:
        for name in DATASETS:
            if not DS[name]["updated"] or time.time() - DS[name]["updated"] > DATASETS[name].get("ttl", DATASET_TTL):
                refresh_dataset(name)
                time.sleep(3)
        time.sleep(1800)


def dataset_payload(name):
    ds = DATASETS.get(name)
    if not ds:
        return {"error": "inconnu"}
    items = DS[name]["items"]
    if name == "agenda":  # les rendez-vous passés disparaissent sans attendre la collecte suivante
        items = [i for i in items if i["ts"] >= time.time() - 6 * 3600]
    return {"name": name, "title": ds["title"], "source": ds["source"], "url": ds["url"], "note": ds["note"],
            "year": YEAR, "updated": DS[name]["updated"], "total": len(items), "victimes": sum(i.get("victimes", 1) for i in items),
            "ref": max([i.get("n", 0) for i in items] or [0]) or None,
            "unlocated": sum(1 for i in items if i.get("lat") is None and not i.get("nat")), "items": items}



# ---------------------------------------------------------------------------
# SEO / GEO : pages rendues côté serveur (indexables), sitemap, FAQ + données structurées
# ---------------------------------------------------------------------------
BASE_URL = os.environ.get("BASE_URL", "https://oraclemarin.fr/cabrule")
BASE_PATH = os.environ.get("BASE_PATH", "/cabrule")
SITE_NAME = "ça brûle"
ICON_LINKS = ('<link rel="icon" href="%(b)s/favicon.ico" sizes="any"><link rel="icon" type="image/png" sizes="192x192" href="%(b)s/icon-192.png">'
              '<link rel="apple-touch-icon" href="%(b)s/apple-touch-icon.png"><link rel="manifest" href="%(b)s/manifest.webmanifest">'
              '<link rel="alternate" type="application/rss+xml" href="%(b)s/rss.xml" title="ça brûle : toutes les mobilisations">') % {"b": BASE_PATH}
HOME_TITLE = "Carte des manifestations et contestations en France aujourd'hui, en direct | ça brûle"
HOME_DESC = ("Carte en direct des manifestations, grèves, blocus lycéens, blocages et mobilisations en France aujourd'hui, ville par ville, d'après la presse locale. Filtrez par thème (lycéens, travail, agriculteurs…), recevez un flux RSS. Mise à jour toutes les 10 minutes.")

FAQ = [
    ("Où ont lieu les manifestations aujourd'hui en France ?",
     "La carte « ça brûle » affiche les villes où la presse signale une manifestation, une grève, un blocage ou un rassemblement "
     "dans les dernières 24 heures. Plus la bulle est grande, plus il y a d'articles. Cliquez sur une ville ou cherchez la vôtre "
     "avec la barre de recherche pour lire les articles."),
    ("D'où viennent les données de la carte ?",
     "Des titres d'articles de la presse en ligne : flux de ici (ex-France Bleu), La Dépêche, Ouest-France, Le Monde, franceinfo, "
     "Le Télégramme, actu.fr et d'autres, ainsi que Google Actualités, Bing Actualités, GDELT et Mastodon. La commune citée dans "
     "le titre est détectée automatiquement grâce à la base officielle des communes (geo.api.gouv.fr)."),
    ("À quelle fréquence la carte est-elle mise à jour ?",
     "Toutes les 10 minutes pour les mobilisations en direct. En zoomant sur un secteur, ou en cherchant une ville, la carte interroge "
     "en plus les communes visibles. Les jeux de données complémentaires (féminicides conjugaux, décès liés à la police, accidents du travail mortels) sont actualisés une fois par jour."),
    ("La carte est-elle exhaustive et fiable ?",
     "Non, elle est indicative. Elle ne montre que ce que la presse en ligne relaie, avec des erreurs de localisation possibles. "
     "Une mention dans un titre n'est pas une confirmation : ouvrez l'article source pour vérifier. Pour une information officielle, "
     "consultez la préfecture ou le ministère de l'Intérieur."),
    ("Comment savoir s'il y a une manifestation ou un blocage dans ma ville ?",
     "Tapez le nom de votre ville dans la barre de recherche : la carte s'y place et interroge la presse pour cette commune, même petite. "
     "Si aucun signal n'apparaît, aucune mobilisation n'a été relayée par la presse ces dernières 24 heures."),
    ("Que sont les modes « Féminicides », « Décès police » et « Accidents du travail » ?",
     "Ce sont trois cartes complémentaires, basées sur des recensements publics : les féminicides conjugaux comptés par le collectif "
     "Féminicides par compagnons ou ex, les décès liés à des interventions des forces de l'ordre recensés par Désarmons-les, et les accidents du travail mortels "
     "recensés par le compte « Accident du travail : silence, des ouvriers meurent ». "
     "Les noms des victimes sont retirés et les positions sont ramenées au centre de la commune."),
]

FKEY = {}
PAGE_CSS = ("""
:root{--bg:#0f1114;--panel:#171a1f;--line:#2a2f37;--fg:#e8eaed;--mute:#8b929c;--fire:#ff5a1f;--fire2:#ffb020}
*{box-sizing:border-box}html{background:var(--bg)}
body{margin:0;color:var(--fg);font:16px/1.6 system-ui,sans-serif;background:var(--bg)}
header,main,footer{max-width:820px;margin:0 auto;padding:0 18px}
header{padding-top:18px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-size:24px;font-weight:700;letter-spacing:-.5px;color:var(--fg);text-decoration:none}.brand span{color:var(--fire)}
.btn{background:var(--fire);color:#fff;text-decoration:none;padding:9px 16px;border-radius:99px;font-size:14px}
h1{font-size:28px;line-height:1.25;margin:22px 0 8px}h2{font-size:19px;margin:28px 0 8px}
a{color:var(--fire2)}.meta{color:var(--mute);font-size:13px}
ul{padding-left:20px}li{margin:8px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin:14px 0}
footer{color:var(--mute);font-size:13px;padding-bottom:36px;margin-top:30px;border-top:1px solid var(--line);padding-top:16px}
""").strip()


def esc(t):
    return html.escape(str(t), quote=True)


def fkey():
    if not FKEY:
        for c in FULL:
            FKEY[(c["nom"], c["dep"])] = c
    return FKEY


def slug(c):
    return "%s-%s" % (norm(c["nom"]).replace(" ", "-").replace("œ", "oe").replace("æ", "ae"), c["code"])


def city_url(c, absolute=False):
    return ("%s/ville/%s" % (BASE_URL if absolute else BASE_PATH, slug(c)))


def iso_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def ago_txt(ts):
    m = max(0, int((time.time() - ts) / 60))
    return "il y a %d min" % m if m < 60 else "il y a %d h" % round(m / 60)


def active_cities():
    """Villes avec au moins un article de moins de 24 h, triées par nombre d'articles."""
    cutoff = time.time() - 86400
    out = []
    with LOCK:
        events = list(STATE["events"])
    for e in events:
        arts = [a for a in e["articles"] if a["ts"] >= cutoff]
        c = fkey().get((e["nom"], e["dep"]))
        if arts and c:
            out.append((c, arts, e["n"]))
    out.sort(key=lambda t: (-t[2], -t[1][0]["ts"]))
    return out


RECENT = {"ts": 0, "d": {}}   # (commune, département) -> nombre d'articles sur 90 jours, recalculé toutes les heures


def recent_cities():
    """Villes couvertes par au moins 2 articles ces 90 derniers jours : leur page reste indexable d'une mobilisation à l'autre
    (une page qui bascule en noindex puis revient sort de l'index de Google et met du temps à y rentrer)."""
    if time.time() - RECENT["ts"] > 3600:
        try:
            with DB_LOCK:
                conn = db()
                try:
                    rows = conn.execute("SELECT nom, dep, COUNT(*) FROM articles WHERE ts >= ? AND nom IS NOT NULL GROUP BY nom, dep", (time.time() - 90 * 86400,)).fetchall()
                finally:
                    conn.close()
            RECENT["d"] = {(n, d): k for n, d, k in rows}
            RECENT["ts"] = time.time()
        except Exception as ex:
            print("villes récentes :", ex)
            RECENT["ts"] = time.time() - 3000   # nouvel essai dans 10 min
    return RECENT["d"]


def indexable(c, n):
    """Évite les pages minces : 2 articles minimum, ou une grande ville, ou une ville déjà couverte (2 articles ou plus) sur 90 jours."""
    return n >= 2 or c["pop"] >= 20000 or recent_cities().get((c["nom"], c["dep"]), 0) >= 2


def faq_ld():
    return {"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in FAQ]}


def home_ld():
    graph = [
        {"@type": "WebSite", "@id": BASE_URL + "/#site", "name": SITE_NAME, "url": BASE_URL + "/", "inLanguage": "fr-FR",
         "description": HOME_DESC},
        {"@type": "WebApplication", "@id": BASE_URL + "/#app", "name": "ça brûle — carte des manifestations", "url": BASE_URL + "/",
         "applicationCategory": "NewsApplication", "operatingSystem": "Web", "inLanguage": "fr-FR", "isAccessibleForFree": True,
         "areaServed": {"@type": "Country", "name": "France"}, "description": HOME_DESC,
         "offers": {"@type": "Offer", "price": "0", "priceCurrency": "EUR"}, "dateModified": iso_now()},
        faq_ld(),
    ]
    return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False)


def about_html():
    act = active_cities()[:24]
    links = " · ".join('<a href="%s">%s</a> <span class="meta">(%d)</span>' % (city_url(c), esc(c["nom"]), n) for c, arts, n in act)
    faq = "".join("<h3>%s</h3><p>%s</p>" % (esc(q), esc(a)) for q, a in FAQ)
    return ('<details id="about"><summary>À propos · questions fréquentes</summary><div class="aboutin">'
            "<h2>Carte des manifestations aujourd'hui en France</h2>"
            "<p>« ça brûle » recense en direct les manifestations, grèves, blocages et rassemblements signalés par la presse, ville par ville, "
            "avec filtres par thème (lycéens, agriculteurs, travail, transports, santé, écologie…).</p>"
            + ("<h3>Mobilisations signalées aujourd'hui</h3><p>%s</p>" % links if links else "")
            + sources_blurb() + "<h3>Suivre les mobilisations</h3><p>"
            '<a href="%s/a-venir">Manifestations à venir</a> · <a href="%s/lyceens">Manifestations et blocus lycéens</a> · <a href="%s/rss.xml">Flux RSS de toutes les mobilisations</a> · '
            '<a href="%s/rss/lyceens.xml">Flux RSS lycéens</a></p>' % (BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH)
            + "<h3>Cartes complémentaires</h3><p>"
            '<a href="%s/feminicides">Féminicides conjugaux en 2026</a> · <a href="%s/deces-police">Décès liés à la police en 2026</a> · <a href="%s/accidents-du-travail">Accidents du travail mortels en 2026</a> · <a href="%s/departements">Par département</a> · <a href="%s/quartiers-prioritaires">Quartiers prioritaires et argent public</a></p>'
              % (BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH)
            + '<p>Les plans sociaux, fermetures d\'usines et faillites d\'entreprises derrière beaucoup de mobilisations : '
              '<a href="https://oraclemarin.fr/plans-sociaux/">Plans sociaux et faillites en France</a>, suivi quotidien et sourcé.</p>'
            + faq + '<p class="meta">Pour les modèles de langage : <a href="%s/llms.txt">llms.txt</a>.</p></div></details>' % BASE_PATH)


def render_index():
    t = open(os.path.join(HERE, "static", "index.html"), encoding="utf-8").read()
    t = t.replace("<!--LDJSON-->", '<script type="application/ld+json">%s</script>' % home_ld().replace("</", "<\\/"))
    return t.replace("<!--ABOUT-->", about_html())


def _shell(title, desc, canonical, body, robots="index,follow", ld=None):
    ldtag = '<script type="application/ld+json">%s</script>' % json.dumps(ld, ensure_ascii=False).replace("</", "<\\/") if ld else ""
    return ('<!doctype html><html lang="fr"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>%s</title><meta name=\"description\" content=\"%s\">"
            '<link rel="canonical" href="%s"><meta name="robots" content="%s,max-image-preview:large">'
            '<meta name="theme-color" content="#0f1114">%s'
            '<meta property="og:type" content="website"><meta property="og:locale" content="fr_FR"><meta property="og:site_name" content="ça brûle">'
            '<meta property="og:title" content="%s"><meta property="og:description" content="%s"><meta property="og:url" content="%s">'
            '<meta property="og:image" content="%s/og.png"><meta name="twitter:card" content="summary_large_image">'
            "<style>%s</style>%s</head><body>"
            '<header><a class="brand" href="%s/">ça <span>brûle</span></a><a class="btn" href="%s/">Ouvrir la carte</a></header>'
            "<main>%s</main>"
            '<footer>Carte indicative construite à partir des titres de la presse en ligne : une mention n\'est pas une confirmation, vérifiez à la source. '
            '<a href="%s/">Carte des manifestations</a> · <a href="%s/a-venir">Manifestations à venir</a> · <a href="/cabrule/cadrage">Cadrage des médias</a> · <a href="%s/feminicides">Féminicides</a> · <a href="%s/deces-police">Décès liés à la police</a> · <a href="%s/accidents-du-travail">Accidents du travail</a> · '
            '<a href="%s/llms.txt">llms.txt</a></footer></body></html>') % (
        esc(title), esc(desc), esc(canonical), robots, ICON_LINKS, esc(title), esc(desc), esc(canonical), BASE_URL,
        PAGE_CSS, ldtag, BASE_PATH, BASE_PATH, body, BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH)


EDITOR_FOOTER = ('<br><br><strong>Édité par Quentin Closset</strong>, développeur solo à Perros-Guirec · '
                 '<a href="https://oraclemarin.fr/apropos/">À propos</a> · '
                 '<a href="mailto:ADRESSE_DE_CONTACT">Contact</a> · '
                 '<a href="https://buymeacoffee.com/capoupascap" rel="noopener" target="_blank">Soutenir</a> · '
                 '<a href="https://oraclemarin.fr/rgpd/">Mentions légales</a> · '
                 '<a href="https://oraclemarin.fr/">Oracle Marin</a>')


def shell(*a, extra_footer="", **k):
    out = _shell(*a, **k)
    out = out.replace("</head>", '<script src="/om-bar.js?v=2" defer></script></head>', 1)  # menu commun Oracle Marin
    old = '<a href="%s/llms.txt">llms.txt</a></footer>' % BASE_PATH
    out = out.replace(old, '<a href="%s/lyceens">Mobilisations lycéennes</a> · <a href="%s/rss.xml">Flux RSS</a> · '
                      '<a href="%s/sources">Sources et méthode</a> · <a href="%s/departements">Par département</a> · ' % (BASE_PATH, BASE_PATH, BASE_PATH, BASE_PATH)
                      + '<a href="https://oraclemarin.fr/plans-sociaux/">Plans sociaux et faillites</a> · ' + '<a href="https://oraclemarin.fr/ville/">Votre ville en chiffres</a> · <a href="https://oraclemarin.fr/ressources/">Cartes à imprimer pour l’enseignement</a> · ' + old)
    return out.replace("</footer></body>", extra_footer + "</footer></body>") if extra_footer else out


def city_history(c, limit=12):
    """Dernières mobilisations signalées dans une ville (90 jours) : rend utile la page d'une ville sans article aujourd'hui."""
    try:
        with DB_LOCK:
            conn = db()
            try:
                rows = conn.execute("SELECT ts, title, src, link FROM articles WHERE nom = ? AND dep = ? AND ts >= ? ORDER BY ts DESC LIMIT 60",
                                    (c["nom"], c.get("dep"), time.time() - 90 * 86400)).fetchall()
            finally:
                conn.close()
    except Exception:
        return []
    out, vus = [], set()
    for ts, title, src, link in rows:
        k = norm(title or "")[:60]
        if k in vus: continue
        vus.add(k); out.append((ts, title, src, link))
        if len(out) >= limit: break
    return out


def city_page(slug_str):
    code = slug_str.rsplit("-", 1)[-1]
    c = next((x for x in FULL if x["code"] == code), None)
    if not c or slug(c) != slug_str:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,follow")
    mine = next(((arts, n) for cc, arts, n in active_cities() if cc["code"] == code), None)
    url = city_url(c, True)
    label = "%s (%s)" % (c["nom"], c["dep"]) if c.get("dep") else c["nom"]
    others = [(cc, n) for cc, arts, n in active_cities() if cc["code"] != code][:10]
    other_html = ("<h2>Autres villes où une mobilisation est signalée</h2><p>" + " · ".join(
        '<a href="%s">%s</a> <span class="meta">(%d)</span>' % (city_url(cc), esc(cc["nom"]), n) for cc, n in others) + "</p>") if others else ""
    cta = '<p><a class="btn" href="%s/?ville=%s">Voir %s sur la carte interactive</a></p>' % (BASE_PATH, code, esc(c["nom"]))
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "ça brûle", "item": BASE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Manifestations à %s" % c["nom"], "item": url}]},
        {"@type": "WebPage", "name": "Manifestations à %s aujourd'hui" % c["nom"], "url": url, "inLanguage": "fr-FR",
         "dateModified": iso_now(), "about": {"@type": "Place", "name": c["nom"], "address": {"@type": "PostalAddress", "addressCountry": "FR"},
                                             "geo": {"@type": "GeoCoordinates", "latitude": c["lat"], "longitude": c["lon"]}}}]}
    if mine:
        arts, n = mine
        items = "".join('<li><a href="%s" rel="noopener" target="_blank">%s</a> <span class="meta">— %s, %s</span></li>' % (
            esc(a["link"]), esc(a["title"]), esc(a.get("src", "")), ago_txt(a["ts"])) for a in arts[:15])
        body = ("<h1>Manifestations et mobilisations à %s aujourd'hui</h1>"
                '<p class="meta">Mis à jour %s · carte actualisée toutes les 10 minutes</p>'
                '<div class="card"><p><strong>%d article%s de presse</strong> signale%s une mobilisation (manifestation, grève, blocage ou rassemblement) '
                "à %s au cours des dernières 24 heures.</p></div><ul>%s</ul>%s%s") % (
            esc(label), time.strftime("%d/%m/%Y à %H:%M UTC", time.gmtime()), n, "s" if n > 1 else "", "nt" if n > 1 else "",
            esc(c["nom"]), items, cta, other_html)
        title = "Manifestation aujourd'hui à %s : blocages, grèves, rassemblements | ça brûle" % c["nom"]
        desc = "%d article%s de presse signale%s une mobilisation à %s (%s) ces dernières 24 h. Carte en direct des manifestations en France." % (
            n, "s" if n > 1 else "", "nt" if n > 1 else "", c["nom"], c.get("dep", ""))
        return 200, shell(title, desc, url, body, "index,follow" if indexable(c, n) else "noindex,follow", ld)
    hist = city_history(c) if recent_cities().get((c["nom"], c.get("dep")), 0) >= 2 else []
    if hist:   # ville déjà couverte ces 90 jours : page utile et indexable d'une mobilisation à l'autre
        items = "".join('<li><a href="%s" rel="noopener" target="_blank">%s</a> <span class="meta">— %s, %s</span></li>' % (
            esc(l), esc(t), esc(sr or ""), time.strftime("%d/%m/%Y", time.localtime(ts))) for ts, t, sr, l in hist)
        body = ("<h1>Manifestations à %s aujourd'hui</h1><div class=\"card\"><p>Aucune manifestation, grève ou blocage n'est signalé à %s "
                "par la presse en ligne au cours des dernières 24 heures. Cette page se met à jour dès qu'une mobilisation est signalée.</p></div>"
                "<h2>Dernières mobilisations signalées à %s</h2><ul>%s</ul>%s%s") % (esc(label), esc(c["nom"]), esc(c["nom"]), items, cta, other_html)
        return 200, shell("Manifestation à %s aujourd'hui : dernières mobilisations | ça brûle" % c["nom"],
                          "Aucune mobilisation signalée à %s ces dernières 24 h. Les %d dernières manifestations, grèves et blocages relevés dans la presse en ligne." % (c["nom"], len(hist)),
                          url, body, "index,follow", ld)
    body = ("<h1>Manifestations à %s aujourd'hui</h1><div class=\"card\"><p>Aucune manifestation, grève ou blocage n'est signalé à %s "
            "par la presse en ligne au cours des dernières 24 heures.</p></div>%s%s") % (esc(label), esc(c["nom"]), cta, other_html)
    return 200, shell("Manifestation à %s aujourd'hui | ça brûle" % c["nom"], "Aucune mobilisation signalée à %s ces dernières 24 h." % c["nom"],
                      url, body, "noindex,follow", ld)


def dataset_page(kind):
    name = {"feminicides": "feminicides", "accidents-du-travail": "travail"}.get(kind, "police")
    d = dataset_payload(name)
    items = d["items"]
    year = d["year"]
    title = {"feminicides": "Féminicides conjugaux en France en %d : carte et décompte | ça brûle",
             "travail": "Accidents du travail mortels en France en %d : carte et décompte | ça brûle",
             "police": "Décès liés à des interventions policières en France en %d : carte | ça brûle"}[name] % year
    if name == "feminicides":
        desc = ("%d féminicides conjugaux recensés en France depuis le 1er janvier %d par le collectif Féminicides par compagnons ou ex. "
                "Carte par commune et liste, actualisées une fois par jour.") % (d["total"], year)
    elif name == "travail":
        desc = ("%d accidents du travail mortels recensés en France depuis le 1er janvier %d d'après la presse, par le compte « Accident du travail : silence, des ouvriers meurent ». "
                "Carte par commune et liste avec les articles sources, actualisées une fois par jour.") % (d["total"], year)
    else:
        desc = ("%d décès survenus lors ou à la suite d'interventions des forces de l'ordre en France depuis le 1er janvier %d, "
                "recensés par Désarmons-les. Carte par commune et liste, actualisées une fois par jour.") % (d.get("victimes") or d["total"], year)
    url = "%s/%s" % (BASE_URL, {"feminicides": "feminicides", "travail": "accidents-du-travail", "police": "deces-police"}[name])
    lis = []
    for i in items:
        dt = time.strftime("%Y-%m-%d", time.localtime(i["ts"]))
        lieu = ("%s (%s)" % (i["place"], i["dep"])) if i.get("place") else "lieu non localisé"
        txt = (i.get("age", "") + " " if i.get("age") else "") + i["text"]
        pub = (' · <a href="%s" rel="noopener" target="_blank">publication</a>' % esc(i["post"])) if i.get("post") and i["post"] != i["url"] else ""
        lis.append('<li><time datetime="%s">%s</time> — <strong>%s</strong> — %s <a href="%s" rel="noopener" target="_blank">source</a>%s</li>' % (
            dt, time.strftime("%d/%m/%Y", time.localtime(i["ts"])), esc(lieu), esc(txt), esc(i["url"]), pub))
    help_ = ('<div class="card"><strong>Besoin d\'aide ?</strong> En cas de violences conjugales : 3919 (gratuit, anonyme) ; en danger immédiat : 17 ou 112, '
             'par SMS au 114.</div>') if name == "feminicides" else ""
    if name == "travail":
        help_ = ('<div class="card">Le compteur du compte indique <strong>%s morts recensés</strong> en %d. Cette page reprend les publications que le robot sait lire '
                 '(%d dont %d localisées sur la carte) : le décompte exact est à lire sur <a href="%s" rel="noopener" target="_blank">le compte lui-même</a>. '
                 'Les données sont reprises avec l\'accord de son auteur.</div>') % (d.get("ref") or "—", year, d["total"], d["total"] - d["unlocated"], esc(d["url"]))
    body = ("<h1>%s</h1><p class=\"meta\">Source : <a href=\"%s\" rel=\"noopener\" target=\"_blank\">%s</a> · actualisé %s</p>"
            '<div class="card"><p><strong>%s</strong> depuis le 1er janvier %d. %s</p></div>%s'
            '<p><a class="btn" href="%s/?mode=%s">Voir la carte interactive</a></p><ul>%s</ul>') % (
        esc(title.split(" | ")[0]), esc(d["url"]), esc(d["source"]), ago_txt(d["updated"]) if d["updated"] else "—", (("%d victimes dans %d cas" % (d["victimes"], d["total"])) if d.get("victimes") and d["victimes"] != d["total"] else ("%d cas" % d["total"])), year,
        esc(d["note"]), help_, BASE_PATH, name, "".join(lis))
    ld = {"@context": "https://schema.org", "@type": "Dataset", "name": title.split(" | ")[0], "description": desc, "url": url,
          "inLanguage": "fr-FR", "isBasedOn": d["url"], "creator": {"@type": "Organization", "name": d["source"], "url": d["url"]},
          "temporalCoverage": "%d-01-01/%d-12-31" % (year, year), "spatialCoverage": {"@type": "Country", "name": "France"},
          "dateModified": iso_now(), "isAccessibleForFree": True}
    return 200, shell(title, desc, url, body, "index,follow", ld)


JOURS = "lundi mardi mercredi jeudi vendredi samedi dimanche".split()
MOIS_TXT = "janvier février mars avril mai juin juillet août septembre octobre novembre décembre".split()


def jour_txt(ts):
    t = time.localtime(ts)
    return "%s %d %s" % (JOURS[t.tm_wday], t.tm_mday, MOIS_TXT[t.tm_mon - 1])


def agenda_page():
    d = dataset_payload("agenda")
    items = d["items"]
    title = "Manifestations à venir en France : agenda des prochaines mobilisations | ça brûle"
    desc = ("%d manifestations, rassemblements et grèves annoncés en France dans les 60 prochains jours, ville par ville, "
            "d'après les agendas militants, les syndicats et la presse locale. Actualisé toutes les heures.") % d["total"]
    url = BASE_URL + "/a-venir"
    nat = [i for i in items if i.get("nat")]
    days, lis = {}, []
    for i in items:
        if not i.get("nat"):
            days.setdefault(time.strftime("%Y-%m-%d", time.localtime(i["ts"])), []).append(i)
    for day, its in sorted(days.items()):
        lis.append("<h3>%s</h3><ul>%s</ul>" % (esc(jour_txt(its[0]["ts"]).capitalize()), "".join(
            '<li><time datetime="%s">%s</time> — <strong>%s (%s)</strong> — %s <a href="%s" rel="noopener nofollow" target="_blank">%s</a></li>' % (
                time.strftime("%Y-%m-%d" if i.get("day") else "%Y-%m-%dT%H:%M", time.localtime(i["ts"])),
                "dans la journée" if i.get("day") else time.strftime("%Hh%M", time.localtime(i["ts"])),
                esc(i["place"]), esc(i["dep"]), esc(i["text"]), esc(i["url"]),
                esc("article (%s)" % i["press"]) if i.get("press") else "annonce") for i in its)))
    natd = {}
    for i in nat:
        natd.setdefault(time.strftime("%Y-%m-%d", time.localtime(i["ts"])), []).append(i)
    natl = ("<h2>Appels nationaux</h2><ul>%s</ul>" % "".join(
        '<li><time datetime="%s">%s</time> — %s (%s)</li>' % (
            day, esc(jour_txt(g[0]["ts"])), esc(g[0]["text"]), ", ".join('<a href="%s" rel="noopener nofollow" target="_blank">%s</a>' % (
                esc(i["url"]), esc(i.get("org") or "annonce")) for i in g)) for day, g in sorted(natd.items()))) if nat else ""
    body = ("<h1>Manifestations à venir en France</h1><p class=\"meta\">Source : <a href=\"%s\" rel=\"noopener\" target=\"_blank\">%s</a> · actualisé %s</p>"
            '<div class="card"><p><strong>%d rendez-vous</strong> annoncés dans les 60 prochains jours. %s</p></div>'
            '<p><a class="btn" href="%s/?mode=agenda">Voir sur la carte</a></p>%s<h2>Par jour</h2>%s') % (
        esc(d["url"]), esc(d["source"]), ago_txt(d["updated"]) if d["updated"] else "—", d["total"], esc(d["note"]),
        BASE_PATH, natl, "".join(lis) or "<p>Aucun rendez-vous annoncé pour l'instant.</p>")
    ld = {"@context": "https://schema.org", "@type": "ItemList", "name": "Manifestations à venir en France", "url": url,
          "numberOfItems": len(items), "itemListElement": [
              {"@type": "ListItem", "position": n + 1, "item": {
                  "@type": "Event", "name": i["text"], "startDate": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(i["ts"])),
                  "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode", "url": i["url"],
                  "location": {"@type": "Place", "name": i["place"] or "France",
                               "address": {"@type": "PostalAddress", "addressLocality": i["place"] or "France", "addressCountry": "FR"}}}}
              for n, i in enumerate(items[:50])]}
    return 200, shell(title, desc, url, body, "index,follow", ld)


THEME_LABELS = {"lyceens": "Lycéens / étudiants", "agri": "Agriculteurs", "travail": "Travail / grèves", "transports": "Transports",
                "sante": "Santé", "ecologie": "Écologie", "international": "International", "politique": "Budget / politique",
                "violences": "Heurts / violences", "securite_logement": "Logement / quartiers"}


def recent_items(theme=None, limit=100):
    """Articles des dernières 24 h (tous, ou d'un thème), du plus récent au plus ancien : (commune, article)."""
    cutoff = time.time() - 86400
    with LOCK:
        events = list(STATE["events"])
    out = []
    for e in events:
        for a in e["articles"]:
            if a["ts"] >= cutoff and (theme is None or theme in (a.get("themes") or [])):
                out.append((e, a))
    out.sort(key=lambda x: -x[1]["ts"])
    return out[:limit]


def rss_xml(theme=None):
    from email.utils import format_datetime
    name = THEME_LABELS.get(theme)
    title = "ça brûle : " + ("mobilisations « %s »" % name if name else "mobilisations en France")
    self_url = BASE_URL + ("/rss/%s.xml" % theme if theme else "/rss.xml")
    items = []
    for e, a in recent_items(theme):
        link = BASE_URL + "/go/" + a["h"]
        items.append("<item><title>%s</title><link>%s</link><guid isPermaLink=\"false\">%s</guid><pubDate>%s</pubDate>"
                     "<description>%s</description></item>" % (
                         esc("%s (%s) : %s" % (e["nom"], e.get("dep") or "", a["title"])), esc(link), esc(a["h"]),
                         format_datetime(datetime.fromtimestamp(a["ts"], timezone.utc)),
                         esc("Source : %s. Titre de presse signalant une mobilisation à %s ; à vérifier à la source." % (a.get("src") or "presse", e["nom"]))))
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
            "<title>%s</title><link>%s/</link><description>%s</description><language>fr-FR</language>"
            '<atom:link href="%s" rel="self" type="application/rss+xml"/>%s</channel></rss>') % (
        esc(title), BASE_URL, esc("Manifestations, grèves, blocus et rassemblements signalés par la presse en France, sur les dernières 24 heures."),
        esc(self_url), "".join(items))


def lyceens_page():
    items = recent_items("lyceens", 40)
    cities = collections.Counter((e["nom"], e.get("dep") or "") for e, a in items)
    url = BASE_URL + "/lyceens"
    title = "Manifestations et blocus lycéens en France aujourd'hui | ça brûle"
    desc = ("Les mobilisations de lycéens et d'étudiants signalées par la presse en France ces dernières 24 heures : blocus, manifestations, "
            "rassemblements, ville par ville. Carte en direct et flux RSS.")
    lis = "".join('<li><a href="%s/go/%s" rel="noopener nofollow" target="_blank">%s</a> <span class="meta">— %s (%s), %s, %s</span></li>' % (
        BASE_PATH, esc(a["h"]), esc(a["title"]), esc(e["nom"]), esc(e.get("dep") or ""), esc(a.get("src") or ""), ago_txt(a["ts"]))
        for e, a in items)
    top = " · ".join("%s <span class=\"meta\">(%d)</span>" % (esc(n), k) for (n, d), k in cities.most_common(15))
    body = ("<h1>Manifestations et blocus lycéens en France aujourd'hui</h1>"
            '<p class="meta">Mis à jour %s · actualisé toutes les 10 minutes</p>'
            '<div class="card"><p><strong>%d article%s de presse</strong> signale%s une mobilisation de lycéens ou d\'étudiants '
            "sur les dernières 24 heures, dans <strong>%d commune%s</strong>.</p>%s</div>"
            "%s"
            '<p><a class="btn" href="%s/?theme=lyceens">Voir les mobilisations lycéennes sur la carte</a> '
            '<a class="btn" href="%s/rss/lyceens.xml">Flux RSS</a></p>'
            "<h2>Comment lire cette page</h2><p>Chaque ligne est un titre de presse en ligne qui parle d'un blocus, d'une manifestation ou d'un rassemblement "
            "de lycéens ou d'étudiants. Le lien ouvre l'article d'origine. Une mention n'est pas une confirmation : vérifiez à la source. "
            'Les autres thèmes (travail, agriculteurs, transports, santé, écologie…) sont sur <a href="%s/">la carte générale</a>.</p>') % (
        time.strftime("%d/%m/%Y à %H:%M UTC", time.gmtime()), len(items), "s" if len(items) > 1 else "", "nt" if len(items) > 1 else "",
        len(cities), "s" if len(cities) > 1 else "", ("<p>Communes : %s</p>" % top) if top else "",
        ("<ul>%s</ul>" % lis) if lis else "<p>Aucune mobilisation lycéenne signalée pour le moment.</p>", BASE_PATH, BASE_PATH, BASE_PATH)
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "ça brûle", "item": BASE_URL + "/"},
            {"@type": "ListItem", "position": 2, "name": "Mobilisations lycéennes", "item": url}]},
        {"@type": "CollectionPage", "name": "Manifestations et blocus lycéens en France", "url": url, "inLanguage": "fr-FR", "dateModified": iso_now()}]}
    return 200, shell(title, desc, url, body, "index,follow", ld)


CORRECTIONS = [  # (date, description) : à compléter à chaque erreur signalée puis corrigée
    ("2026-10-08", "Ouverture du journal. Limite connue : le filtre « lycéens » peut classer à tort des articles qui ne les concernent pas (motifs larges)."),
]


def corrections_html():
    # le résultat passe par un « % » de mise en forme dans presse_page : on double les pourcentages
    return ("<ul>" + "".join("<li><strong>%s</strong> : %s</li>" % (esc(d), esc(t)) for d, t in sorted(CORRECTIONS, reverse=True)) + "</ul>").replace("%", "%%")


def presse_page():
    url = BASE_URL + "/presse"
    title = "Dossier de presse : ça brûle, une carte faite seul avec des flux RSS publics et citée par ChatGPT"
    desc = ("Éléments de preuve, méthode et limites : comment une carte des manifestations lancée début octobre 2026 par un développeur indépendant "
            "se retrouve citée par ChatGPT à côté de médias établis.")
    body = (
        "<h1>ça brûle : éléments de preuve, méthode et limites</h1>"
        '<p class="meta">Page rédigée par l\'éditeur du site, mise à jour le 8 octobre 2026. Les chiffres sont des mesures personnelles, pas un audit indépendant.</p>'
        '<div class="card"><p><strong>En une phrase :</strong> un seul développeur, une centaine de flux RSS publics et aucun budget publicitaire ont suffi pour produire une carte '
        "qu'un assistant IA cite en réponse à « une carte des manifestations aujourd'hui ? ». La question qui en découle concerne les rédactions : sur quels critères "
        "les assistants choisissent-ils leurs sources, et comment rester la source citée ?</p></div>"
        "<h2>Le site</h2>"
        "<p>« ça brûle » est une carte en direct des manifestations, grèves, blocus et rassemblements en France. Elle est alimentée par les titres de la presse en ligne "
        "(flux RSS, agrégateurs d'actualité) : le site ne copie pas les articles, il ne garde que le titre, la source, la date et le lien vers l'article d'origine. "
        'La liste des sources et la méthode sont publiques : <a href="%(b)s/sources">sources et méthode</a>. Mise en ligne début octobre 2026, éditée par '
        'un développeur indépendant à Perros-Guirec.</p>'
        "<h2>L'observation</h2>"
        "<p>Le 8 octobre 2026, sur une session vierge de ChatGPT, à la question « une carte des manifestations en France aujourd'hui ? », la réponse renvoie vers la carte "
        "(source affichée : le nom de domaine, « Oracle Marin »), et cite aussi Le Monde pour le contexte du mouvement lycéen. Capture ci-dessous, telle qu'elle est apparue.</p>"
        '<p><img src="%(b)s/preuve-chatgpt-2026-10-08.png" alt="Réponse de ChatGPT du 8 octobre 2026 citant la carte ça brûle et Le Monde" width="445" height="353" '
        'style="max-width:100%%;height:auto;border-radius:8px;border:1px solid #2a2f37"></p>'
        "<p>Sur les essais manuels de l'éditeur, la carte est citée environ une fois sur deux. C'est un petit nombre d'essais : il ne s'agit pas d'un taux mesuré. "
        "Une réponse de ChatGPT varie d'une session à l'autre.</p>"
        "<h2>Ce qui a été fait</h2>"
        "<p>Rien d'exotique : une page par ville rendue côté serveur, avec des données structurées JSON-LD et un canonical ; un plan de site ; une page de sources et de méthode ; "
        "un fichier llms.txt ; une notification IndexNow aux moteurs qui la supportent ; des mises à jour fréquentes (toutes les 10 minutes) ; une version anglaise avec hreflang ; "
        "des flux RSS par thème.</p>"
        "<h2>Les mesures</h2>"
        "<ul>"
        "<li><strong>Search Console (domaine oraclemarin.fr, 3 mois jusqu'au 5 octobre)</strong> : 80 clics et 1 560 impressions au total, CTR moyen 5,1 %%, position moyenne 13,5. "
        "Avant le 1er octobre : 0 à 3 clics par jour. Le 5 octobre : environ 17 clics et 350 impressions. La courbe porte sur l'ensemble du domaine, qui regroupe d'autres outils de l'éditeur.</li>"
        "<li><strong>Journaux du serveur, du 2 au 8 octobre</strong> (adresses IP distinctes par jour, visiteurs probablement humains, sans cookie ni traceur) : "
        "111, 30, 13, 33, 92, 89 puis 85 le 8 octobre à la mi-journée. Les pics suivent l'actualité lycéenne et un post Reddit.</li>"
        "<li><strong>Origine du trafic</strong> : sur ces sept jours, les adresses venues de ChatGPT (repérées par le paramètre <code>utm_source=chatgpt.com</code> que l'assistant ajoute à ses liens, "
        "sans referrer) représentent 112 adresses-jours (30, 6, 5, 14, 16, 17 et 24). Google arrive depuis le 5 octobre (11, 53, 35 puis 19 adresses par jour).</li>"
        "<li><strong>Reddit</strong> : le post de présentation a atteint environ 18 000 vues, 15 votes et 13 commentaires (statistiques Reddit de l'éditeur, 8 octobre).</li>"
        "<li><strong>Robots</strong> : des robots d'assistants (Claude, ChatGPT-User, OAI-SearchBot) lisent des pages du site depuis sa mise en ligne.</li>"
        "</ul>"
        "<h2>Ce que ça ne prouve pas</h2>"
        "<ul>"
        "<li>Un seul site, quelques jours : aucune conclusion statistique.</li>"
        "<li>Un sujet d'actualité très porteur (les mobilisations lycéennes) : la hausse de trafic peut venir de l'actualité plus que des techniques employées. "
        "Le trafic venu des messageries s'est déjà tassé.</li>"
        "<li>La carte répond à un besoin que les médias ne couvrent pas (une carte en direct). Elle n'est pas « devant » eux sur l'information : dans la même réponse, un média est cité pour le contexte.</li>"
        "<li>Les causes ne sont pas établies : les mesures montrent une coïncidence avec la mise en ligne, pas un lien démontré avec chaque technique.</li>"
        "</ul>"
        "<h2>Les enjeux que cela soulève</h2>"
        "<p>Les moteurs de réponse retiennent des sources selon des critères mal connus : structure des pages, fraîcheur, clarté de la source. Ces leviers sont à la portée de petits acteurs, "
        "et aussi de quiconque voudrait diffuser de fausses informations avec une apparence de sérieux. Pour les médias, la question est de savoir comment rester la source citée "
        "quand une partie de l'audience passe par un assistant, et comment vérifier ce que ces assistants relaient.</p>"
        "<h2>Fiabilité du site</h2>"
        "<p>Une mention dans la presse n'est pas une confirmation : la carte l'indique partout. Des erreurs de localisation sont possibles (par exemple un nom de personne "
        "qui ressemble à celui d'une commune). Les corrections se font en signalant l'erreur à l'éditeur.</p>"
        "<h2>Journal des corrections</h2>"
        "<p>Les erreurs de lieu signalées sont corrigées et listées ici avec leur date. Pour en signaler une : ADRESSE_DE_CONTACT.</p>"
        + corrections_html() +
        "<h2>Qui</h2>"
        '<p>Quentin Closset, développeur indépendant à Perros-Guirec. <a href="https://www.linkedin.com/in/quentin-closset-5a0853409/" rel="me noopener">LinkedIn</a>. '
        "Autres réalisations en ligne : <a href=\"https://oraclemarin.fr/apropos/\">oraclemarin.fr/apropos</a>.</p>"
        "<h2>Contact</h2>"
        "<p>Les mesures détaillées (Search Console, journaux, passages de robots) peuvent être communiquées sur demande, ainsi que la méthode des essais.</p>"
    ) % {"b": BASE_PATH}
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title, "url": url, "inLanguage": "fr-FR",
          "dateModified": "2026-10-08", "isPartOf": {"@id": BASE_URL + "/#site"}}
    return 200, shell(title, desc, url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


ORG_NAMES = {"Force Ouvrière", "FSU", "SNES-FSU", "UNSA", "Solidaires", "Ligue des droits de l'Homme", "CGT", "Attac", "Greenpeace France", "Oxfam France"}
AGGREGATORS = [
    {"name": "Google Actualités", "domain": "news.google.com", "kind": "agregateur", "note": "Recherches par mots-clés et par ville, plafonnées et ralenties automatiquement en cas de limitation."},
    {"name": "Bing Actualités", "domain": "bing.com", "kind": "agregateur", "note": "Repli quand Google est indisponible."},
    {"name": "GDELT", "domain": "gdeltproject.org", "kind": "agregateur", "note": "Base ouverte d'actualité mondiale."},
    {"name": "Mastodon (hashtags)", "domain": "mastodon.social", "kind": "reseau_social", "note": "Publications publiques, à vérifier : source la moins fiable."},
]


def source_registry():
    """Registre public des sources : une ligne par flux lu directement, avec son type et sa reconnaissance par la CPPAP."""
    cppap_by_host = {f["host"]: f for f in CPPAP_FEEDS}
    out, seen = [], set()
    for u in FEEDS:
        host = urllib.parse.urlparse(u).netloc
        name = FEED_NAMES.get(host, host.replace("www.", ""))
        dom = host.replace("www.", "")
        key = (name, dom)
        if key in seen:
            continue
        seen.add(key)
        reco = dom in CPPAP_DOMAINS or any(dom.endswith("." + d) for d in CPPAP_DOMAINS)
        if name in ORG_NAMES:
            kind = "organisation_engagee"
        elif host in cppap_by_host:
            kind = "presse_reconnue_cppap"
        elif reco:
            kind = "presse_reconnue_cppap"
        else:
            kind = "presse"
        row = {"name": name, "domain": dom, "kind": kind, "feed": u, "cppap_registered": bool(reco)}
        if host in cppap_by_host:
            row["cppap_number"] = cppap_by_host[host].get("cppap") or None
            row["departement"] = cppap_by_host[host].get("dep") or None
        out.append(row)
    out.sort(key=lambda r: (r["kind"], r["name"].lower()))
    return {"generated": iso_now(), "count": len(out), "method": BASE_URL + "/sources",
            "kinds": {"presse_reconnue_cppap": "service de presse en ligne inscrit à la CPPAP (registre du ministère de la Culture)",
                      "presse": "titre de presse sans inscription CPPAP vérifiée ici",
                      "organisation_engagee": "syndicat ou association : communiqués de leur propre organisation, source engagée par nature",
                      "agregateur": "moteur ou base d'actualité utilisé en complément", "reseau_social": "publications publiques, non vérifiées"},
            "sources": out, "aggregators": AGGREGATORS}


def sitemap_xml():
    now = time.strftime("%Y-%m-%d", time.gmtime())
    urls = [(BASE_URL + "/", "hourly", "1.0"), (BASE_URL + "/feminicides", "daily", "0.6"), (BASE_URL + "/deces-police", "daily", "0.6"), (BASE_URL + "/accidents-du-travail", "daily", "0.6"),
            (BASE_URL + "/a-venir", "daily", "0.8"),
            (BASE_URL + "/sources", "daily", "0.5"), (BASE_URL + "/lyceens", "hourly", "0.8"), (BASE_URL + "/presse", "weekly", "0.4"), (BASE_URL + "/integrer", "monthly", "0.5")]
    try:
        urls += [(BASE_URL + "/departements", "daily", "0.8"), (BASE_URL + "/quartiers-prioritaires", "weekly", "0.7"), (BASE_URL + "/argent-quartiers", "weekly", "0.8"), (BASE_URL + "/habitant-type", "weekly", "0.7"), (BASE_URL + "/cadrage", "daily", "0.7"), (BASE_URL + "/franceinfo", "daily", "0.6")] + [(dep_url(c, True), "daily", "0.6") for c in dep_names()]
    except Exception as ex:
        print("sitemap départements :", ex)
    actives = {(c["nom"], c["dep"]) for c, arts, n in active_cities()}
    urls += [(city_url(c, True), "hourly", "0.7") for c, arts, n in active_cities() if indexable(c, n)]
    urls += [(city_url(c, True), "daily", "0.5") for (nom, dep), k in recent_cities().items()   # villes récemment couvertes mais sans article aujourd'hui
             if k >= 2 and (nom, dep) not in actives and (nom, dep) in fkey() for c in [fkey()[(nom, dep)]]]
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join("<url><loc>%s</loc><lastmod>%s</lastmod><changefreq>%s</changefreq><priority>%s</priority></url>" % (esc(u), now, f, p)
                      for u, f, p in urls) + "</urlset>")



SOURCE_CHANNELS = [
    ("Flux de presse lus directement (RSS)", [
        ("ici (ex-France Bleu) : 45 radios locales", "https://www.ici.fr/"),
        ("La Dépêche", "https://www.ladepeche.fr/"), ("Sud Ouest", "https://www.sudouest.fr/"),
        ("Le Télégramme", "https://www.letelegramme.fr/"), ("Ouest-France", "https://www.ouest-france.fr/"),
        ("actu.fr", "https://actu.fr/"), ("Le Monde", "https://www.lemonde.fr/"),
        ("franceinfo", "https://www.francetvinfo.fr/"), ("20 Minutes", "https://www.20minutes.fr/"),
        ("BFMTV", "https://www.bfmtv.com/"), ("Le Figaro", "https://www.lefigaro.fr/"),
        ("L'Express", "https://www.lexpress.fr/"), ("Libération", "https://www.liberation.fr/")]),
    ("Agrégateurs d'actualités interrogés par mots-clés", [
        ("Google Actualités (recherche générale et ville par ville, plafonnée)", "https://news.google.com/"),
        ("Bing Actualités", "https://www.bing.com/news"),
        ("GDELT (base mondiale d'articles de presse)", "https://www.gdeltproject.org/")]),
    ("Réseaux sociaux ouverts", [
        ("Mastodon : messages publics sous les hashtags blocus, manifestation, blocage, grève, mobilisation", "https://mastodon.social/")]),
    ("Cartes complémentaires (recensements associatifs publics)", [
        ("Collectif Féminicides par compagnons ou ex", "https://www.feminicides.fr/"),
        ("Désarmons-les", "https://desarmons.net/"),
        ("Accident du travail : silence, des ouvriers meurent (Bluesky)", "https://bsky.app/profile/accidentdutravail.bsky.social")]),
    ("Référentiels", [
        ("Base officielle des communes (geo.api.gouv.fr)", "https://geo.api.gouv.fr/"),
        ("Fond de carte © OpenStreetMap", "https://www.openstreetmap.org/copyright")]),
]


SOURCE_CHANNELS.insert(1, ("Presse régionale (RSS)", [
    ("Le Dauphiné libéré", "https://www.ledauphine.com/"), ("Le Progrès", "https://www.leprogres.fr/"),
    ("Midi Libre", "https://www.midilibre.fr/"), ("Nice-Matin", "https://www.nicematin.com/"),
    ("L'Indépendant", "https://www.lindependant.fr/"), ("Dernières Nouvelles d'Alsace", "https://www.dna.fr/"),
    ("L'Alsace", "https://www.lalsace.fr/"), ("L'Est Républicain", "https://www.estrepublicain.fr/"),
    ("Le Républicain Lorrain", "https://www.republicain-lorrain.fr/"), ("Vosges Matin", "https://www.vosgesmatin.fr/"),
    ("Le Bien Public", "https://www.bienpublic.com/"), ("Le Journal de Saône-et-Loire", "https://www.lejsl.com/"),
    ("Charente Libre", "https://www.charentelibre.fr/"), ("France 3 Régions", "https://france3-regions.franceinfo.fr/"),
    ("Marsactu", "https://marsactu.fr/"), ("Rue89 Lyon", "https://www.rue89lyon.fr/"),
    ("Rue89 Strasbourg", "https://www.rue89strasbourg.com/"), ("Rue89 Bordeaux", "https://rue89bordeaux.com/"),
    ("Médiacités", "https://www.mediacites.fr/")]))
SOURCE_CHANNELS.insert(2, ("Presse indépendante et militante (RSS)", [
    ("Reporterre", "https://reporterre.net/"), ("Basta!", "https://basta.media/"), ("Rapports de Force", "https://rapportsdeforce.fr/"),
    ("Contre-Attaque", "https://contre-attaque.net/"), ("L'Humanité", "https://www.humanite.fr/"), ("Mediapart (titres publics)", "https://www.mediapart.fr/")]))
SOURCE_CHANNELS.insert(3, ("Médias nationaux et internationaux (RSS supplémentaires)", [
    ("RFI", "https://www.rfi.fr/"), ("France 24", "https://www.france24.com/fr/"), ("Europe 1", "https://www.europe1.fr/"),
    ("CNews", "https://www.cnews.fr/"), ("Euronews", "https://fr.euronews.com/")]))

SOURCE_CHANNELS.insert(4, ("Presse spécialisée, régionale et outre-mer (RSS)", [
    ("L'Étudiant", "https://www.letudiant.fr/"), ("Café pédagogique", "https://www.cafepedagogique.net/"),
    ("Terre-net (agriculture)", "https://www.terre-net.fr/"), ("actu.fr Île-de-France", "https://actu.fr/ile-de-france/"),
    ("La 1ère Nouvelle-Calédonie", "https://la1ere.francetvinfo.fr/nouvellecaledonie/"),
    ("Le Monde : Société, Politique, Éducation", "https://www.lemonde.fr/"), ("franceinfo : Éducation, Transports", "https://www.francetvinfo.fr/"),
    ("Le Figaro : France", "https://www.lefigaro.fr/")]))
SOURCE_CHANNELS.insert(4, ("Presse reconnue par la CPPAP : titres locaux et nationaux (RSS)", sorted(
    [(f["nom"], "https://" + f["host"].replace("www.", "") + "/") for f in CPPAP_FEEDS], key=lambda t: t[0].lower())))
SOURCE_CHANNELS.insert(6, ("Syndicats et associations : communiqués de leurs propres organisations (RSS)", [
    ("Force Ouvrière", "https://www.force-ouvriere.fr/"), ("CGT", "https://www.cgt.fr/"), ("FSU", "https://fsu.fr/"),
    ("SNES-FSU", "https://www.snes.edu/"), ("UNSA", "https://www.unsa.org/"), ("Solidaires", "https://solidaires.org/"),
    ("Ligue des droits de l'Homme", "https://www.ldh-france.org/"), ("Attac", "https://france.attac.org/"),
    ("Greenpeace France", "https://www.greenpeace.fr/"), ("Oxfam France", "https://www.oxfamfrance.org/")]))


def source_counts():
    """Médias dont au moins un article de moins de 24 h est affiché, avec le nombre d'articles."""
    cutoff = time.time() - 86400
    groups = {}
    with LOCK:
        vals = list(STORE.values())
    for v in vals:
        a = v["a"]
        if a["ts"] < cutoff or (a.get("avenir") or "") > day_of(time.time()):
            continue
        name = (a.get("src") or "").strip() or "Source non précisée"
        g = groups.setdefault(norm(name).replace(".fr", "").replace(".com", ""), {"n": 0, "names": {}})
        g["n"] += 1
        g["names"][name] = g["names"].get(name, 0) + 1
    out = [(max(g["names"], key=g["names"].get), g["n"]) for g in groups.values()]
    return sorted(out, key=lambda kv: (-kv[1], kv[0].lower()))


def sources_blurb():
    sc = source_counts()
    if not sc:
        return ""
    top = ", ".join("%s (%d)" % (esc(n), c) for n, c in sc[:12])
    return ("<h3>Sources agrégées</h3><p>%d médias ces dernières 24 h, dont : %s. "
            '<a href="%s/sources">Voir toutes les sources et la méthode</a>.</p>') % (len(sc), top, BASE_PATH)


def sources_page():
    sc = source_counts()
    total = sum(c for _, c in sc)
    url = BASE_URL + "/sources"
    chan = "".join("<h2>%s</h2><ul>%s</ul>" % (esc(t), "".join(
        '<li><a href="%s" rel="noopener" target="_blank">%s</a></li>' % (esc(u), esc(n)) for n, u in items)) for t, items in SOURCE_CHANNELS)
    rows = "".join("<li>%s <span class=\"meta\">— %d article%s</span></li>" % (esc(n), c, "s" if c > 1 else "") for n, c in sc[:120])
    body = (
        "<h1>Sources et méthode : d'où viennent les données de la carte</h1>"
        "<p>Par transparence, voici tout ce que « ça brûle » agrège, comment les articles sont retenus et ce que la carte ne fait pas.</p>"
        "<h2>Médias dont des articles sont affichés en ce moment</h2>"
        '<p class="meta">%d articles de %d médias sur les dernières 24 heures · mis à jour %s</p><ul>%s</ul>'
        "%s"
        "<h2>Méthode</h2>"
        "<p>Toutes les 10 minutes, la carte lit les flux de presse ci-dessus et interroge les agrégateurs. Seuls les articles dont le <strong>titre</strong> "
        "contient un terme de mobilisation sont retenus : manifestation, manifestants, grève, blocage, blocus, rassemblement, mobilisation, "
        "piquet, émeutes, heurts, occupation, barrage, syndicat… Les titres sportifs ou culturels (concert, festival, exposition, football…) sont écartés.</p>"
        "<p>Quand un visiteur cherche une ville ou zoome, la carte interroge cette commune en cascade : Google Actualités tant que son plafond de requêtes le permet, puis Bing Actualités, "
        "puis, en dernier recours, les articles déjà collectés dans les flux de presse (la source principale). Le plafond de requêtes Google s'ajuste "
        "automatiquement : il baisse dès que Google limite l'accès et remonte progressivement ensuite. Une source bloquée est mise en pause automatiquement "
        "et les autres prennent le relais.</p>"
        "<p>La commune citée dans le titre est repérée automatiquement avec la base officielle des communes. Les doublons de titres sont supprimés. "
        "Un article reste affiché 24 heures après sa publication. Chaque article est classé dans un ou plusieurs thèmes par des listes de mots-clés (sans IA).</p>"
        "<p>Les syndicats et associations listés ci-dessus publient leurs propres communiqués : ce sont des sources premières pour les appels à "
        "mobilisation, mais elles sont engagées par nature, à lire comme telles.</p>"
        "<p>La carte ne conserve que le titre, la source, la date et le lien : elle ne copie pas le contenu des articles, qui restent la propriété de leurs éditeurs.</p>"
        "<h2>Vidéos</h2><p>Le mode « Vidéos » a sa propre liste de sources et sa méthode : <a href=\"sources-videos\">sources des vidéos</a>.</p>"
        "<h2>Ce que la carte ne fait pas</h2>"
        "<p>Elle n'utilise ni compte privé, ni messagerie, ni contenu payant contourné. Elle ne suit aucune personne et ne localise pas les forces de l'ordre. "
        "Elle ne dit pas si une mobilisation est importante ou légitime : elle indique seulement combien d'articles en parlent, et où.</p>"
        "<h2>Limites</h2>"
        "<p>La presse ne relaie pas tout, et une mention dans un titre n'est pas une confirmation. Des erreurs de localisation sont possibles, "
        "et certaines sources peuvent être momentanément indisponibles ou limiter les requêtes. Pour une information officielle, "
        "consultez la préfecture ou le ministère de l'Intérieur.</p>") % (
        total, len(sc), time.strftime("%d/%m/%Y à %H:%M UTC", time.gmtime()), rows or "<li>Aucune donnée pour l'instant.</li>", chan)
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": "Sources et méthode de la carte des manifestations", "url": url,
          "inLanguage": "fr-FR", "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"},
          "publisher": {"@type": "Person", "name": "Quentin Closset", "url": "https://oraclemarin.fr/apropos/"}}
    return 200, shell("Sources et méthode : d'où viennent les données de la carte des manifestations | ça brûle",
                      "Médias agrégés en direct, flux de presse, agrégateurs, méthode de sélection des articles et limites de la carte des manifestations en France.",
                      url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)



# ---------------------------------------------------------------------------
# Widget « féminicides conjugaux » à intégrer (iframe) + page de présentation
# ---------------------------------------------------------------------------
WIDGET_CSS = """
:root{--bg:#0f1114;--fg:#e8eaed;--mute:#9aa1ab;--line:#2a2f37;--acc:#b66cff}
.light{--bg:#ffffff;--fg:#1c1f24;--mute:#5d6672;--line:#dfe3e8;--acc:#7a2fd0}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{padding:14px 16px;border:1px solid var(--line);border-radius:12px;margin:2px}
.k{margin:0;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--mute)}
.n{margin:2px 0 0;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}.n b{font-size:44px;line-height:1;color:var(--acc)}.n span{color:var(--mute)}
.s{margin:4px 0 8px;font-size:13px;color:var(--mute)}
#bars{display:flex;align-items:flex-end;gap:4px;height:54px;margin:6px 0}
#bars div{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%;font-size:9px;color:var(--mute)}
#bars i{display:block;width:100%;background:var(--acc);border-radius:3px 3px 0 0;min-height:2px;opacity:.85}
#map{height:330px;border-radius:8px;margin:8px 0;border:1px solid var(--line)}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:var(--bg);color:var(--fg)}.leaflet-popup-content{margin:10px 12px;font-size:12px;line-height:1.4}.leaflet-popup-content p{margin:4px 0}
.w-police{--acc:#6b95ff}.light.w-police{--acc:#2f5fe0}
.w-travail{--acc:#f0b429}.light.w-travail{--acc:#a86f00}
.dark .leaflet-tile{filter:invert(1) hue-rotate(180deg) brightness(.85) contrast(.9) saturate(.5)}
.src,.help,.by{margin:6px 0 0;font-size:12px;color:var(--mute)}.help b{color:var(--fg)}
a{color:var(--acc)}
"""


WIDGET_CFG = {
    "feminicides": {"kicker": "Féminicides conjugaux · France", "api": "feminicides", "title": "Féminicides conjugaux en France",
                    "source_name": "Collectif Féminicides par compagnons ou ex", "source_url": "https://www.feminicides.fr/",
                    "source_tail": " · décompte a minima, actualisé chaque jour.", "help": True, "full": "/feminicides", "cls": ""},
    "police": {"kicker": "Décès liés aux forces de l'ordre · France", "api": "police", "title": "Décès liés aux forces de l'ordre en France",
               "source_name": "Désarmons-les", "source_url": "https://desarmons.net/listes-des-victimes/personnes-tuees-par-les-forces-de-lordre/",
               "source_tail": " · recensement actualisé chaque jour.", "help": False, "full": "/deces-police", "cls": "w-police"},
    "travail": {"kicker": "Accidents du travail mortels · France", "api": "travail", "title": "Accidents du travail mortels en France",
                "source_name": "Accident du travail : silence, des ouvriers meurent", "source_url": "https://bsky.app/profile/accidentdutravail.bsky.social",
                "source_tail": " · décompte a minima d'après la presse, actualisé chaque jour.", "help": False, "full": "/accidents-du-travail", "cls": "w-travail"},
}


def widget_html(key="feminicides", theme="light", show_map=True):
    cfg = WIDGET_CFG[key]
    theme = "dark" if theme == "dark" else "light"
    api = BASE_PATH + "/api/dataset?name=" + cfg["api"]
    leaflet = ('<link rel="stylesheet" href="%s/lib/leaflet.css"><script src="%s/lib/leaflet.js"></script>' % (BASE_PATH, BASE_PATH)) if show_map else ""
    mapdiv = '<div id="map" aria-label="Carte des cas, positions ramenées au centre de la commune"></div>' if show_map else ""
    helpp = ('<p class="help">En danger ou besoin d\'aide : <b>3919</b> (gratuit, anonyme) · <b>17</b> ou <b>112</b> · SMS <b>114</b></p>') if cfg["help"] else ""
    return ('<!doctype html><html lang="fr" class="%(theme)s %(cls)s"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
            "<title>%(title)s</title><style>%(css)s</style>%(leaflet)s</head><body>"
            '<main role="region" aria-label="%(title)s">'
            '<p class="k">%(kicker)s · <span id="yr"></span></p>'
            '<p class="n"><b id="tot">…</b><span>depuis le 1er janvier</span></p><p class="s" id="last"></p><p class="s" id="unl"></p>'
            '<div id="bars" aria-label="Nombre de cas par mois"></div>%(mapdiv)s'
            '<p class="src">Source : <a href="%(surl)s" target="_blank" rel="noopener">%(sname)s</a>%(stail)s</p>%(help)s'
            '<p class="by"><a href="%(base)s%(full)s" target="_blank" rel="noopener">Voir la carte complète</a> · propulsé par '
            '<a href="%(base)s/" target="_blank" rel="noopener">ça brûle</a></p></main>'
            "<script>(function(){var MAP=%(map)s,ACC=(getComputedStyle(document.documentElement).getPropertyValue('--acc')||'#9b4dff').trim();"
            "fetch('%(api)s').then(function(r){return r.json()}).then(function(d){"
            "var it=d.items||[];document.getElementById('yr').textContent=d.year;var V=d.victimes||d.total;document.getElementById('tot').textContent=V;"
            "if(d.unlocated)document.getElementById('unl').textContent='(dont '+d.unlocated+' non localisé'+(d.unlocated>1?'s':'')+' sur la carte)';"
            "if(V!==d.total)document.getElementById('unl').textContent=(document.getElementById('unl').textContent+' (dans '+d.total+' cas)').trim();"
            "if(it[0]){var t=new Date(it[0].ts*1000);document.getElementById('last').textContent='Dernier cas recensé : '+t.toLocaleDateString('fr-FR',{day:'numeric',month:'long'})"
            "+(it[0].place?' · '+it[0].place+(it[0].dep?' ('+it[0].dep+')':''):'')}"
            "var per=Array.apply(null,Array(12)).map(function(){return 0});it.forEach(function(i){per[new Date(i.ts*1000).getMonth()]++});"
            "var up=new Date().getMonth(),mx=Math.max.apply(null,per.slice(0,up+1).concat([1])),MO='JFMAMJJASOND',h='';"
            "for(var k=0;k<=up;k++){h+='<div title=\"'+per[k]+'\"><i style=\"height:'+Math.round(per[k]/mx*36+2)+'px\"></i>'+MO[k]+'</div>'}"
            "document.getElementById('bars').innerHTML=h;"
            "if(MAP&&window.L){var m=L.map('map',{zoomControl:true,scrollWheelZoom:false,dragging:!L.Browser.mobile,tap:false,minZoom:4,maxZoom:12}).fitBounds([[41.2,-5.3],[51.2,9.7]]);"
            "m.on('focus',function(){m.scrollWheelZoom.enable()});m.on('blur',function(){m.scrollWheelZoom.disable()});"
            "L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:12,attribution:'© OpenStreetMap'}).addTo(m);"
            "it.forEach(function(i){if(i.lat==null)return;var el=document.createElement('div'),b=document.createElement('b'),p=document.createElement('p');"
            "b.textContent=new Date(i.ts*1000).toLocaleDateString('fr-FR',{day:'numeric',month:'long'})+(i.age?' · '+i.age:'')+(i.place?' · '+i.place+(i.dep?' ('+i.dep+')':''):'');el.appendChild(b);"
            "var tx=i.text||'';p.textContent=tx.slice(0,220)+(tx.length>220?'…':'');el.appendChild(p);"
            "if(/^https?:[/][/]/.test(i.url||'')){var a=document.createElement('a');a.href=i.url;a.target='_blank';a.rel='noopener';a.textContent='Lire la source';el.appendChild(a)}"
            "L.circleMarker([i.lat,i.lon],{radius:6,color:ACC,weight:1,fillColor:ACC,fillOpacity:.75}).bindPopup(el,{maxWidth:240}).addTo(m)})}"
            "}).catch(function(){document.getElementById('tot').textContent='—'})})();</script></body></html>") % {
        "theme": theme, "cls": cfg["cls"], "title": html.escape(cfg["title"]), "css": WIDGET_CSS, "leaflet": leaflet, "mapdiv": mapdiv,
        "kicker": html.escape(cfg["kicker"]), "surl": cfg["source_url"], "sname": html.escape(cfg["source_name"]), "stail": cfg["source_tail"],
        "help": helpp, "api": api, "base": BASE_URL, "full": cfg["full"], "map": "true" if show_map else "false"}


LIVE_REGIONS = {  # clé -> (libellé, départements, cadrage [[sud, ouest], [nord, est]])
    "france": ("France", None, [[41.2, -5.3], [51.2, 9.7]]),
    "bretagne": ("Bretagne", ["22", "29", "35", "56"], [[47.15, -5.3], [49.0, -0.95]]),
}


ONLY_SOURCES = {"telegramme": "Le Télégramme", "ouestfrance": "Ouest-France"}  # filtre par média : widget aux couleurs d'un seul titre


def manifs_widget_html(region="france", theme="light", show_map=True, only=""):
    label, deps, bounds = LIVE_REGIONS.get(region) or LIVE_REGIONS["france"]
    only = only if only in ONLY_SOURCES else ""
    media = ONLY_SOURCES.get(only, "")
    theme = "dark" if theme == "dark" else "light"
    leaflet = ('<link rel="stylesheet" href="%s/lib/leaflet.css"><script src="%s/lib/leaflet.js"></script>' % (BASE_PATH, BASE_PATH)) if show_map else ""
    mapdiv = '<div id="map" aria-label="Carte des mobilisations signalées par la presse"></div>' if show_map else ""
    tpl = (
        '<!doctype html><html lang="fr" class="@@THEME@@ w-live"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
        "<title>Mobilisations @@LABEL@@, en direct</title><style>@@CSS@@.w-live{--acc:#ff5a1f}.light.w-live{--acc:#d9420a}"
        ".lst{list-style:none;margin:6px 0 0;padding:0;font-size:13px}.lst li{padding:5px 0;border-top:1px solid var(--line)}"
        ".lst a{color:var(--fg);text-decoration:none}.lst a:hover{text-decoration:underline}.lst small{color:var(--mute)}</style>@@LEAFLET@@</head><body>"
        '<main role="region" aria-label="Mobilisations @@LABEL@@, en direct">'
        '<p class="k">Mobilisations signalées @@WHO@@ · @@LABEL@@ · dernières 24 h</p>'
        '<p class="n"><b id="tot">…</b><span id="lab">article(s)</span></p><p class="s" id="last"></p>@@MAPDIV@@'
        '<ul class="lst" id="lst"></ul>'
        '<p class="src">@@SRCNOTE@@ Chaque lien renvoie à l\'article d\'origine. Une mention n\'est pas une confirmation.</p>'
        '<p class="by"><a href="@@BASE@@/" target="_blank" rel="noopener">Voir la carte complète</a> · propulsé par '
        '<a href="@@BASE@@/" target="_blank" rel="noopener">ça brûle</a></p></main>'
        "<script>(function(){var MAP=@@MAP@@,DEPS=@@DEPS@@,ONLY=@@ONLY@@,B=@@BOUNDS@@,BASE='@@BASE@@',ACC=(getComputedStyle(document.documentElement).getPropertyValue('--acc')||'#ff5a1f').trim();"
        "function ago(ts){var m=Math.max(0,Math.round((Date.now()/1000-ts)/60));return m<60?'il y a '+m+' min':'il y a '+Math.round(m/60)+' h'}"
        "fetch('@@API@@').then(function(r){return r.json()}).then(function(d){"
        "function nz(t){return (t||'').normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase().replace(/[^a-z]/g,'')}"
        "var cut=Date.now()/1000-86400,ev=[],all=[];(d.events||[]).forEach(function(e){if(DEPS&&DEPS.indexOf(String(e.dep))<0)return;"
        "var a=e.articles.filter(function(x){return x.ts>=cut&&(!ONLY||nz(x.src).indexOf(ONLY)>=0)});if(!a.length)return;ev.push({e:e,a:a});a.forEach(function(x){all.push({x:x,e:e})})});"
        "var n=all.length;document.getElementById('tot').textContent=n;"
        "document.getElementById('lab').textContent=(n>1?'articles':'article')+' dans '+ev.length+' commune'+(ev.length>1?'s':'');"
        "all.sort(function(p,q){return q.x.ts-p.x.ts});"
        "if(all[0])document.getElementById('last').textContent='Dernier signalement : '+all[0].e.nom+' · '+ago(all[0].x.ts);"
        "var ul=document.getElementById('lst');all.slice(0,6).forEach(function(p){var li=document.createElement('li'),a=document.createElement('a'),s=document.createElement('small');"
        "a.href=BASE+'/go/'+p.x.h;a.target='_blank';a.rel='noopener nofollow';a.textContent=p.x.title.slice(0,140)+(p.x.title.length>140?'…':'');"
        "s.textContent=' — '+p.e.nom+' · '+(p.x.src||'presse')+' · '+ago(p.x.ts);li.appendChild(a);li.appendChild(s);ul.appendChild(li)});"
        "if(MAP&&window.L){var m=L.map('map',{zoomControl:true,scrollWheelZoom:false,dragging:!L.Browser.mobile,tap:false,minZoom:4,maxZoom:12}).fitBounds(B);"
        "m.on('focus',function(){m.scrollWheelZoom.enable()});m.on('blur',function(){m.scrollWheelZoom.disable()});"
        "L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:12,attribution:'© OpenStreetMap'}).addTo(m);"
        "ev.forEach(function(o){var el=document.createElement('div'),b=document.createElement('b');b.textContent=o.e.nom+' · '+o.a.length+' article'+(o.a.length>1?'s':'');el.appendChild(b);"
        "o.a.slice(0,4).forEach(function(x){var p=document.createElement('p'),a=document.createElement('a');a.href=BASE+'/go/'+x.h;a.target='_blank';a.rel='noopener nofollow';"
        "a.textContent=x.title.slice(0,110)+(x.title.length>110?'…':'');p.appendChild(a);var s=document.createElement('small');s.textContent=' '+(x.src||'');p.appendChild(s);el.appendChild(p)});"
        "L.circleMarker([o.e.lat,o.e.lon],{radius:Math.min(16,5+o.a.length*2),color:ACC,weight:1,fillColor:ACC,fillOpacity:.65}).bindPopup(el,{maxWidth:280}).addTo(m)})}"
        "}).catch(function(){document.getElementById('tot').textContent='—'})})();</script></body></html>")
    rep_ = {"@@THEME@@": theme, "@@LABEL@@": html.escape(label), "@@CSS@@": WIDGET_CSS, "@@LEAFLET@@": leaflet, "@@MAPDIV@@": mapdiv,
            "@@WHO@@": ("par " + html.escape(media)) if media else "par la presse", "@@ONLY@@": json.dumps(only or None),
            "@@SRCNOTE@@": ("Source : %s." % html.escape(media)) if media else "Titres de la presse en ligne.",
            "@@BASE@@": BASE_URL, "@@MAP@@": "true" if show_map else "false", "@@DEPS@@": json.dumps(deps), "@@BOUNDS@@": json.dumps(bounds),
            "@@API@@": BASE_PATH + "/api/events?v=2"}
    for k, v in rep_.items():
        tpl = tpl.replace(k, v)
    return tpl


# Catalogue des widgets : pour en ajouter un, ajouter une entrée ici et sa vue dans seo_route.
WIDGETS = [
    {"key": "feminicides", "title": "Féminicides conjugaux en France",
     "short": "Compteur depuis le 1er janvier, histogramme par mois et mini-carte en option. Mis à jour chaque jour.",
     "source": "Collectif Féminicides par compagnons ou ex", "source_url": "https://www.feminicides.fr/",
     "note": "Les chiffres viennent du décompte public du collectif : un décompte a minima établi à partir de la presse, limité aux féminicides conjugaux. "
             "Ce widget n'est ni édité ni validé par le collectif et ne remplace pas ses publications. Aucun nom de victime n'est affiché ; "
             "les positions sont ramenées au centre de la commune ; le 3919, le 17 ou 112 et le 114 par SMS sont rappelés.",
     "map": True, "h": 300, "h_map": 640},
    {"key": "police", "title": "Décès liés aux forces de l'ordre en France",
     "short": "Compteur depuis le 1er janvier, histogramme par mois et carte de France interactive. Mis à jour chaque jour.",
     "source": "Désarmons-les", "source_url": "https://desarmons.net/listes-des-victimes/personnes-tuees-par-les-forces-de-lordre/",
     "note": "", "map": True, "h": 300, "h_map": 640},
    {"key": "travail", "title": "Accidents du travail mortels en France",
     "short": "Compteur depuis le 1er janvier, histogramme par mois et carte de France interactive. Mis à jour chaque jour.",
     "source": "Accident du travail : silence, des ouvriers meurent", "source_url": "https://bsky.app/profile/accidentdutravail.bsky.social",
     "note": "Les chiffres reprennent le recensement du compte Bluesky « Accident du travail : silence, des ouvriers meurent », avec l'accord de son auteur : décompte a minima établi à partir de la presse. "
             "Aucun nom de victime n'est affiché ; les positions sont ramenées au centre de la commune ; chaque cas renvoie à l'article de presse d'origine.",
     "map": True, "h": 300, "h_map": 640},
    {"key": "manifs-bretagne", "path": "manifs", "qs": "region=bretagne&only=telegramme", "title": "Mobilisations en Bretagne, en direct",
     "short": "Carte des manifestations, grèves et blocages signalés par Le Télégramme en Bretagne sur les dernières 24 heures, avec ses derniers articles. Mis à jour toutes les 10 minutes.",
     "source": "Presse en ligne", "source_url": "", "note": "", "map": True, "h": 420, "h_map": 760},
    {"key": "manifs-bretagne-of", "path": "manifs", "qs": "region=bretagne&only=ouestfrance", "title": "Mobilisations en Bretagne, en direct",
     "short": "Carte des manifestations, grèves et blocages signalés par Ouest-France en Bretagne sur les dernières 24 heures, avec ses derniers articles. Mis à jour toutes les 10 minutes.",
     "source": "Presse en ligne", "source_url": "", "note": "", "map": True, "h": 420, "h_map": 760},
]


WIDGET_BY_KEY = {w["key"]: w for w in WIDGETS}

# Une page par destinataire (collectif, association…) : adressée à eux, non indexée, non listée publiquement.
RECIPIENTS = {
    "collectif-feminicides": {"name": "Collectif Féminicides par compagnons ou ex", "pour": "le Collectif Féminicides par compagnons ou ex", "widgets": ["feminicides"]},
    "desarmons-les": {"name": "Désarmons-les", "pour": "Désarmons-les", "widgets": ["police"]},
    "accident-du-travail": {"name": "Accident du travail : silence, des ouvriers meurent", "pour": "le compte « Accident du travail : silence, des ouvriers meurent »", "widgets": ["travail"]},
    "le-telegramme": {"name": "Le Télégramme", "pour": "Le Télégramme", "widgets": ["manifs-bretagne"]},
    "ouest-france": {"name": "Ouest-France", "pour": "Ouest-France", "widgets": ["manifs-bretagne-of"]},
}


def _widget_section(w):
    esc_ = html.escape
    mapopt = ' &nbsp; <label><input type="checkbox" class="o-map" checked> Carte interactive</label>' if w["map"] else ""
    return (
        '<section class="card wg" id="%s" data-src="%s/widget/%s" data-h="%d" data-hmap="%d" data-title="%s">'
        "<h2>%s</h2><p>%s</p>"
        '<p><label><input type="checkbox" class="o-light" checked> Thème clair</label>%s</p>'
        '<iframe class="prev" title="Aperçu : %s" width="100%%" height="%d" style="border:0;max-width:520px"></iframe>'
        '<p><textarea class="code" readonly rows="5" style="width:100%%;max-width:760px;font:13px ui-monospace,Consolas,monospace;'
        'background:#171a1f;color:#e8eaed;border:1px solid #2a2f37;border-radius:8px;padding:10px"></textarea></p>'
        '<p><button class="btn copy" type="button" style="border:0;cursor:pointer">Copier le code</button> <span class="meta ok"></span></p>'
        "</section>" % (w["key"], BASE_URL, w.get("path", w["key"]) + ("?" + w["qs"] if w.get("qs") else ""), w["h"], w["h_map"], esc_(w["title"], quote=True), esc_(w["title"]), esc_(w["short"]),
                        mapopt, esc_(w["title"], quote=True), w["h"]))


WIDGET_JS = (
    "<script>document.querySelectorAll('.wg').forEach(function(w){"
    "var src=w.dataset.src,h0=+w.dataset.h,h1=+w.dataset.hmap,t=w.dataset.title,l=w.querySelector('.o-light'),m=w.querySelector('.o-map'),"
    "p=w.querySelector('.prev'),c=w.querySelector('.code'),ok=w.querySelector('.ok');"
    "function up(){var q=['theme='+(l.checked?'light':'dark')];if(m)q.push('map='+(m.checked?'1':'0'));var u=src+(src.indexOf('?')<0?'?':'&')+q.join('&');"
    "var h=(m&&m.checked)?h1:h0;p.height=h;p.src=u+(u.indexOf('?')<0?'?':'&')+'_p='+Date.now();"
    "c.value='<iframe src=\"'+u+'\" title=\"'+t+'\" width=\"100%\" height=\"'+h+'\" style=\"border:0;max-width:520px\" loading=\"lazy\"></iframe>'}"
    "l.addEventListener('change',up);l.addEventListener('input',up);if(m){m.addEventListener('change',up);m.addEventListener('input',up)}up();"
    "w.querySelector('.copy').onclick=function(){c.select();try{document.execCommand('copy');ok.textContent='Copié.'}catch(e){}}});</script>"
)


def recipient_page(slug):
    r = RECIPIENTS.get(slug)
    if not r:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow")
    name = html.escape(r.get("pour", "le " + r["name"]))
    secs = "".join(_widget_section(WIDGET_BY_KEY[k]) for k in r["widgets"])
    body = (
        "<style>.wg label{cursor:pointer;margin-right:16px;display:inline-flex;align-items:center;gap:6px}"
        ".wg input[type=checkbox]{accent-color:#ff5a1f;width:18px;height:18px;margin:0}</style>"
        "<h1>Un widget pour %s</h1>"
        "<p>Voici de quoi intégrer ce widget sur votre site : il se met à jour tout seul chaque jour. Choisissez les options, "
        "puis copiez le code.</p>" % name
        + secs
        + "<h2>Comment l'ajouter</h2><ul>"
        "<li><strong>WordPress :</strong> bloc « HTML personnalisé », collez le code.</li>"
        "<li><strong>Wix :</strong> « Ajouter » → « Intégrer » → « Code HTML » (ou « Embed »), collez le code.</li>"
        "<li><strong>Autres sites :</strong> collez le code là où le widget doit apparaître.</li></ul>"
        '<p class="meta">Gratuit, sans compte, sans cookie ni traceur. Vous pouvez le retirer à tout moment. '
        "Une autre présentation (couleurs, taille, texte) ? Écrivez-nous, voir en bas de page.</p>" + WIDGET_JS)
    return 200, shell("Widget pour %s | ça brûle" % r.get("pour", "le " + r["name"]), "Widget à intégrer sur votre site.",
                      "%s/widget/pour/%s" % (BASE_URL, slug), body, "noindex,nofollow", None, extra_footer=EDITOR_FOOTER)


def integrer_page():
    url = BASE_URL + "/integrer"
    title = "Intégrer la carte des manifestations sur votre site ou blog (widget gratuit)"
    desc = ("Widget gratuit : la carte en direct des manifestations, grèves et blocages en France, à copier-coller sur votre site, "
            "votre blog ou WordPress. Sans compte, sans cookie. Mis à jour toutes les 10 minutes.")
    body = (
        "<style>#gen label{cursor:pointer;margin-right:16px;display:inline-flex;align-items:center;gap:6px}"
        "#gen select{background:#171a1f;color:#e8eaed;border:1px solid #2a2f37;border-radius:6px;padding:4px 8px;font:inherit}"
        "#gen input[type=checkbox]{accent-color:#ff5a1f;width:18px;height:18px;margin:0}"
        "#code{width:100%;max-width:760px;font:13px ui-monospace,Consolas,monospace;background:#171a1f;color:#e8eaed;"
        "border:1px solid #2a2f37;border-radius:8px;padding:10px}</style>"
        "<h1>Intégrer la carte des manifestations sur votre site</h1>"
        "<p>Gratuit, sans compte, sans cookie : choisissez vos options, copiez le code, collez-le dans votre page. "
        "Le widget se met à jour tout seul toutes les 10 minutes. Il affiche le nombre d'articles de presse signalant une mobilisation "
        "ces dernières 24 heures, les derniers signalements avec le lien vers chaque article, et une carte en option.</p>"
        '<section class="card" id="gen"><h2>Votre widget</h2>'
        '<p><label>Zone <select id="o-region"><option value="france">France entière</option><option value="bretagne">Bretagne</option></select></label>'
        '<label><input type="checkbox" id="o-light" checked> Thème clair</label>'
        '<label><input type="checkbox" id="o-map" checked> Avec la carte</label></p>'
        '<iframe id="prev" title="Aperçu du widget" width="100%" height="760" style="border:0;max-width:520px"></iframe>'
        '<p><textarea id="code" readonly rows="5"></textarea></p>'
        '<p><button class="btn" id="copy" type="button" style="border:0;cursor:pointer">Copier le code</button> <span class="meta" id="ok"></span></p>'
        "</section>"
        "<h2>Comment l'ajouter</h2><ul>"
        "<li><strong>WordPress :</strong> dans l'éditeur, ajoutez un bloc « HTML personnalisé » et collez le code.</li>"
        "<li><strong>Wix :</strong> « Ajouter » → « Intégrer » → « Code HTML », collez le code.</li>"
        "<li><strong>Squarespace :</strong> ajoutez un bloc « Code » et collez le code.</li>"
        "<li><strong>Autres sites ou blogs :</strong> collez le code à l'endroit où le widget doit apparaître. Il s'adapte à la largeur disponible (520 px maximum).</li></ul>"
        "<h2>Bon à savoir</h2><ul>"
        "<li>Le widget ne dépose aucun cookie. Quand la carte est activée, le fond de carte vient d'OpenStreetMap, qui reçoit l'adresse IP du visiteur.</li>"
        "<li>Les titres viennent de la presse en ligne et chaque lien renvoie vers l'article d'origine. Une mention n'est pas une confirmation : le widget "
        "le rappelle. Les données sont indicatives, pas un décompte officiel.</li>"
        "<li>Merci de laisser le lien « propulsé par ça brûle » intégré au widget.</li>"
        "<li>Vous pouvez le retirer à tout moment en supprimant le code de votre page.</li>"
        '<li>Une autre taille, d\'autres couleurs, une zone précise ? Écrivez à <a href="mailto:ADRESSE_DE_CONTACT">ADRESSE_DE_CONTACT</a>.</li></ul>'
        '<p class="meta"><a href="%(b)s/">Retour à la carte</a> · <a href="%(b)s/sources">Sources et méthode</a></p>'
        "<script>(function(){var B=" + json.dumps(BASE_URL) + ",r=document.getElementById('o-region'),l=document.getElementById('o-light'),m=document.getElementById('o-map'),"
        "p=document.getElementById('prev'),c=document.getElementById('code'),ok=document.getElementById('ok');"
        "function up(){var q=['region='+r.value,'theme='+(l.checked?'light':'dark'),'map='+(m.checked?'1':'0')],u=B+'/widget/manifs?'+q.join('&'),h=m.checked?760:420;"
        "p.height=h;p.src=u+'&_p='+Date.now();"
        "c.value='<iframe src=\"'+u+'\" title=\"Mobilisations en direct : ça brûle\" width=\"100%\" height=\"'+h+'\" style=\"border:0;max-width:520px\" loading=\"lazy\"></iframe>'}"
        "[r,l,m].forEach(function(e){e.addEventListener('change',up);e.addEventListener('input',up)});up();"
        "document.getElementById('copy').onclick=function(){c.select();try{document.execCommand('copy');ok.textContent='Copié.'}catch(e){}}})();</script>"
    ).replace("%(b)s", BASE_PATH)
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title, "url": url, "inLanguage": "fr-FR",
          "isPartOf": {"@id": BASE_URL + "/#site"}}
    return 200, shell(title, desc, url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


def videos_sources_page():
    url = BASE_URL + "/sources-videos"
    try: d = json.load(open(os.path.join(HERE, "proto", "sources_videos.json"), encoding="utf-8"))
    except Exception: d = {"suivies": [], "non_suivies": [], "villes": 200, "seuils": {}}
    def ligne(x):
        return '<li><a href="%s" rel="noopener nofollow" target="_blank">%s</a>%s</li>' % (
            esc(x.get("url", "")), esc(x["nom"]), (' <span class="meta">— %d vidéo%s sur la carte</span>' % (x["n"], "s" if x["n"] > 1 else "")) if x.get("n") else "")
    blocs = ""
    for plat, titre in (("Telegram", "Chaînes Telegram publiques"), ("Bluesky", "Comptes Bluesky"), ("YouTube", "Chaînes YouTube (presse régionale, agences, vidéastes de terrain)")):
        l = [x for x in d.get("suivies", []) if x["plateforme"] == plat]
        if l: blocs += "<h3>%s (%d)</h3><ul>%s</ul>" % (titre, len(l), "".join(ligne(x) for x in sorted(l, key=lambda x: x["nom"].lower())))
    non = d.get("non_suivies", [])
    se = d.get("seuils", {})
    body = (
        "<h1>Vidéos des mobilisations : sources et méthode</h1>"
        "<p>Le mode « Vidéos » de la carte rassemble des vidéos de manifestations, grèves, blocages et rassemblements publiées sur les réseaux, "
        "et les place sur la carte d'après le texte qui les accompagne. Voici d'où elles viennent et comment elles sont choisies.</p>"
        "<h2>Sources suivies</h2><p>Ces comptes sont relus à chaque collecte (toutes les 30 minutes) : toutes leurs vidéos qui montrent une mobilisation "
        "en France peuvent apparaître, quel que soit leur nombre de vues.</p>" + (blocs or "<p>Liste en cours de constitution.</p>") +
        "<h2>Sources trouvées par recherche</h2>"
        "<p>Pour couvrir tout le territoire, la carte cherche aussi par mots-clés et hashtags : recherche YouTube « manifestation » sur les %d plus grandes "
        "communes (en rotation, chaque ville environ une fois par jour), recherche YouTube nationale sur les dernières 24 heures, et hashtags Telegram "
        "publics (#manif, #blocus, #lycéens, #grève…). Ces vidéos sont signalées « source non suivie » et n'apparaissent qu'au-delà d'un nombre minimum de vues : "
        "%s vues pour une vidéo de moins de 24 heures, %s sous 72 heures, %s au-delà (%s pour les recherches par ville, où les vidéos locales sont peu vues).</p>"
        % (d.get("villes", 200), se.get("24h", 100), se.get("72h", 300), se.get("ancien", 1000), se.get("villes", 20)) +
        ("<details><summary>Sources non suivies présentes sur la carte en ce moment (%d)</summary><ul>%s</ul></details>" % (len(non), "".join(ligne(x) + "" for x in non))
         if non else "") +
        "<h2>Comment une vidéo est placée</h2>"
        "<p>Un modèle de langue (Gemini) lit le texte du post : commune où la scène a lieu, type d'action (manifestation, blocage, grève, intervention policière…), "
        "pays. Il écarte ce qui ne montre pas une mobilisation réelle (débat en plateau, humour, compilation) et ce qui se passe hors de France. "
        "La commune est ensuite retrouvée dans la base officielle des communes ; le département n'est utilisé que s'il est écrit dans le texte. "
        "Pour les médias locaux, si le texte ne cite aucune commune, la vidéo est placée sur la ville du média et marquée « lieu approximatif ». "
        "Le point est au centre de la commune, pas au lieu exact de la scène. Une même vidéo relayée par plusieurs comptes n'apparaît qu'une fois.</p>"
        "<h2>Ce que la carte ne fait pas</h2>"
        "<p>Elle n'héberge aucune vidéo : elle affiche le lecteur officiel de la plateforme (YouTube, Telegram, Bluesky), chargé seulement quand on appuie sur lecture. "
        "Si l'auteur supprime sa vidéo, elle disparaît aussi de la carte. Elle n'utilise aucun groupe de discussion privé, seulement des chaînes et comptes publics. "
        "Elle ne cherche pas à identifier qui que ce soit.</p>"
        "<h2>Limites</h2>"
        "<p>Une vidéo n'est pas une preuve : des images anciennes ou tournées ailleurs circulent pendant les mobilisations, et la date affichée est celle de la "
        "publication, pas du tournage. Les sources sont de nature très différente (presse, agences, médias militants, particuliers) : à lire comme telles. "
        "Le lieu est déduit automatiquement et peut être faux. Pour signaler une erreur ou une vidéo à retirer : ADRESSE_DE_CONTACT.</p>"
        '<p class="meta"><a href="%(b)s/?mode=videos">Voir les vidéos sur la carte</a> · <a href="%(b)s/sources">Sources et méthode de la carte en direct</a></p>'
    ).replace("%(b)s", BASE_PATH)
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": "Vidéos des mobilisations : sources et méthode", "url": url,
          "inLanguage": "fr-FR", "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"}}
    return 200, shell("Vidéos des manifestations : sources et méthode | ça brûle",
                      "Liste des chaînes et comptes suivis, recherches par mots-clés, méthode de localisation et limites du mode Vidéos de la carte ça brûle.",
                      url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


# ---------------------------------------------------------------------------
# Fiches par département : mobilisations, plans sociaux, accidents du travail, féminicides, décès police sur une même carte
# Chaque couche garde sa source ; les recoupages sont de simples co-présences par commune, jamais une affirmation de lien.
# ---------------------------------------------------------------------------
import csv, io

PS_BASE = "https://oraclemarin.fr/plans-sociaux/"
PS_CSV = PS_BASE + "evenements.csv"
DEP_LAYERS = [  # clé, jeu de données, libellé, couleur
    ("manif", None, "Mobilisations", "#ff5a1f"),
    ("ps", "ps", "Plans sociaux et faillites", "#17c3a2"),
    ("travail", "travail", "Accidents du travail mortels", "#f0b429"),
    ("fem", "feminicides", "Féminicides conjugaux", "#9b4dff"),
    ("police", "police", "Décès liés à la police", "#4b7bff"),
]
DEP_GEO = {}
DEP_NAMES = {}
DEPSTATS = {"ts": 0, "d": {}}
QPV_CSV = os.path.join(HERE, "qpv_pop.csv")
QPV = {}
DEP_POP = {}


def dep_qpv():
    """Population des quartiers prioritaires de la politique de la ville (Insee, population municipale 2024) : {département: {"pop": …, "qpv": [(nom, commune, pop)]}}"""
    if not QPV:
        try:
            for r in csv.DictReader(open(QPV_CSV, encoding="utf-8-sig")):
                e = QPV.setdefault(r["listeDep"].strip(), {"pop": 0, "qpv": []})
                p = int(r["popMuniQPV"] or 0)
                e["pop"] += p
                e["qpv"].append((r["libQPV"], r["ListeLibDepcom"], p))
        except Exception as ex:
            print("QPV :", ex)
    return QPV


def dep_pop(code):
    if not DEP_POP:
        for c in FULL:
            DEP_POP[c["dep"]] = DEP_POP.get(c["dep"], 0) + (c.get("pop") or 0)
    return DEP_POP.get(code, 0)


def frn(n):
    return "{:,}".format(int(n)).replace(",", "\u202f")


def dep_geo():
    if not DEP_GEO:
        g = json.load(open(os.path.join(HERE, "static", "lib", "departements.geojson"), encoding="utf-8"))
        DEP_GEO.update({f["properties"]["code"]: f for f in g["features"]})
    return DEP_GEO


def dep_names():
    if not DEP_NAMES:
        path = os.path.join(HERE, "departements.json")
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            d = {x["code"]: x["nom"] for x in json.loads(http_text("https://geo.api.gouv.fr/departements?fields=nom,code"))}
            open(path, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False))
        DEP_NAMES.update({k: v for k, v in d.items() if k in dep_geo()})
    return DEP_NAMES


def dep_slug(code):
    return code.lower() + "-" + re.sub(r"[^a-z0-9]+", "-", norm(dep_names()[code])).strip("-")


def dep_url(code, absolute=False):
    return "%s/departement/%s" % (BASE_URL if absolute else BASE_PATH, dep_slug(code))


def pl(n, one, many):
    return "%d %s" % (n, one if n <= 1 else many)


def fetch_ps():
    """Annonces de plans sociaux, fermetures et procédures d'entreprises de 50 salariés et plus (12 mois) : données publiées par
    oraclemarin.fr/plans-sociaux (fichier evenements.csv), hors lignes que ce projet écarte lui-même de ses totaux."""
    raw = http_text(PS_CSV, 120).lstrip("﻿")
    cut = time.strftime("%Y-%m-%d", time.localtime(time.time() - 365 * 86400))
    deps = dep_names()
    items = []
    for r in csv.DictReader(io.StringIO(raw)):
        day = (r.get("date") or "")[:10]
        dep = (r.get("departement") or "").strip()
        if r.get("exclu_des_totaux") == "oui" or day < cut or dep not in deps:
            continue
        try:
            ts = time.mktime(time.strptime(day, "%Y-%m-%d")) + 12 * 3600
        except Exception:
            continue
        commune = (r.get("commune") or "").strip()
        c = locate_travail(commune, dep) if commune else None
        n = (r.get("postes_annonces") or "").strip()
        typ = (r.get("type") or "").strip()
        ent = (r.get("entreprise") or "").strip()
        items.append({"ts": ts, "lat": c["lat"] if c else None, "lon": c["lon"] if c else None,
                      "place": c["nom"] if c else commune, "dep": dep, "type": typ, "entreprise": ent,
                      "postes": int(n) if n.isdigit() else None,
                      "text": ("%s : %s" % (ent, typ) if ent else typ) + (" (%s postes annoncés)" % n if n.isdigit() and int(n) > 0 else ""),
                      "url": (r.get("url_source") or "").strip() or PS_BASE})
    return items


def dep_manif(code, days=30):
    """Articles de presse signalant une mobilisation dans le département : (lieux avec leurs articles, tous les articles)."""
    try:
        with DB_LOCK:
            conn = db()
            try:
                rows = conn.execute("SELECT ts, title, src, link, nom, lat, lon, h, themes FROM articles WHERE dep = ? AND ts >= ? ORDER BY ts DESC",
                                    (code, time.time() - days * 86400)).fetchall()
            finally:
                conn.close()
    except Exception:
        rows = []
    seen, places, arts = set(), {}, []
    for ts, title, src, link, nom, lat, lon, h, th in rows:
        if h in seen or not nom:
            continue
        seen.add(h)
        try:
            themes = json.loads(th or "[]")
        except Exception:
            themes = []
        a = {"ts": ts, "t": title or "", "s": src or "", "u": link or "%s/go/%s" % (BASE_PATH, h), "th": themes, "p": nom}
        p = places.setdefault(nom, {"place": nom, "lat": lat, "lon": lon, "n": 0, "items": []})
        p["n"] += 1
        p["items"].append(a)
        arts.append(a)
    return list(places.values()), arts


def dep_stats():
    """Compteurs par département pour la carte d'ensemble (recalculés toutes les 10 minutes)."""
    if DEPSTATS["d"] and time.time() - DEPSTATS["ts"] < 600:
        return DEPSTATS["d"]
    out = {c: {"manif": 0, "ps": 0, "travail": 0, "fem": 0, "police": 0, "postes": 0} for c in dep_names()}
    try:
        with DB_LOCK:
            conn = db()
            try:
                for dep, n in conn.execute("SELECT dep, COUNT(DISTINCT h) FROM articles WHERE ts >= ? AND nom IS NOT NULL GROUP BY dep",
                                           (time.time() - 30 * 86400,)).fetchall():
                    if dep in out:
                        out[dep]["manif"] = n
            finally:
                conn.close()
    except Exception as ex:
        print("stats départements :", ex)
    for key, ds, _, _ in DEP_LAYERS:
        if not ds:
            continue
        for i in DS[ds]["items"]:
            d = str(i.get("dep"))
            if d in out:
                out[d][key] += 1
                if key == "ps":
                    out[d]["postes"] += i.get("postes") or 0
    for c in out:
        q, p = dep_qpv().get(c, {}).get("pop", 0), dep_pop(c)
        out[c]["qpvpop"] = q
        out[c]["qpv"] = round(100.0 * q / p, 1) if p else 0
    DEPSTATS["d"], DEPSTATS["ts"] = out, time.time()
    return out


DEP_CSS = """
.dh h1{margin:6px 0 4px;font-size:34px;letter-spacing:-.5px}.dh h1 small{font-size:15px;color:var(--mute);font-weight:500;border:1px solid var(--line);border-radius:99px;padding:2px 10px;vertical-align:middle;margin-left:6px}
.lead{color:#c9ced6;margin:6px 0 14px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;margin:14px 0}
.tile{display:block;text-decoration:none;color:var(--fg);background:var(--panel);border:1px solid var(--line);border-top:3px solid var(--c);border-radius:12px;padding:12px 14px;transition:transform .15s,border-color .15s}
.tile:hover{transform:translateY(-2px);border-color:var(--c)}
.tile b{display:block;font-size:34px;line-height:1.05;color:var(--c);text-shadow:0 0 18px var(--c)}
.tile span{display:block;font-size:13px;margin-top:2px}.tile small{display:block;color:var(--mute);font-size:12px;margin-top:2px}
.mapcard{position:relative;border:1px solid var(--line);border-radius:14px;overflow:hidden;margin:16px 0;background:#0b0d10}
#dmap,#fmap{height:min(64vh,580px);min-height:380px;background:#0b0d10}
.dark-tiles{filter:invert(1) hue-rotate(180deg) brightness(.85) contrast(.9) saturate(.5)}
.chips{position:absolute;z-index:500;left:10px;top:10px;display:flex;flex-wrap:wrap;gap:6px;max-width:calc(100% - 70px)}
.chip{cursor:pointer;border:1px solid var(--line);background:rgba(15,17,20,.88);color:var(--fg);border-radius:99px;padding:5px 11px;font:inherit;font-size:12.5px;display:inline-flex;align-items:center;gap:6px;backdrop-filter:blur(4px)}
.chip i{width:10px;height:10px;border-radius:50%;background:var(--c);box-shadow:0 0 8px var(--c)}
.chip.off{opacity:.45}.chip.off i{box-shadow:none}
.pm{border-radius:50%;background:radial-gradient(circle,rgba(255,255,255,.75),var(--c) 45%,transparent 72%);box-shadow:0 0 16px var(--c);animation:pp 2.4s ease-in-out infinite}
@keyframes pp{0%{transform:scale(.8);opacity:1}70%{transform:scale(1.15);opacity:.75}100%{transform:scale(.8);opacity:1}}
.leaflet-popup-content-wrapper,.leaflet-popup-tip{background:var(--panel);color:var(--fg)}
.leaflet-popup-content{max-height:38vh;overflow:auto;font-size:13px;line-height:1.4}.leaflet-popup-content a{color:var(--fire2)}.leaflet-popup-content p{margin:5px 0}
.leaflet-tooltip{background:var(--panel);color:var(--fg);border:1px solid var(--line);box-shadow:none}
.cross{display:grid;gap:10px;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));padding:0;list-style:none}
.cross li{margin:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
.cross b{display:block;font-size:16px}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;background:var(--c);box-shadow:0 0 8px var(--c)}
.cross span{display:block;font-size:13px;color:#c9ced6;margin-top:3px}
details.lst{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:4px 16px;margin:12px 0}
details.lst>summary{cursor:pointer;padding:10px 0;font-weight:600;list-style:none}details.lst>summary::-webkit-details-marker{display:none}
details.lst>summary:before{content:"";display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--c);box-shadow:0 0 8px var(--c);margin-right:10px}
details.lst>summary small{color:var(--mute);font-weight:400;margin-left:6px}
.dtab{width:100%;border-collapse:collapse;font-size:14px}.dtab th,.dtab td{padding:7px 8px;border-top:1px solid var(--line);text-align:right}
.dtab th:first-child,.dtab td:first-child{text-align:left}.dtab th{color:var(--mute);font-weight:500;font-size:12.5px;position:sticky;top:0;background:var(--bg)}
.dtab a{color:var(--fg);text-decoration:none}.dtab a:hover{color:var(--fire2)}.dtab .z{color:#4c5460}
.mt{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 12px}
.mt .chip{background:var(--panel)}.mt .chip.on{border-color:var(--c);box-shadow:0 0 0 1px var(--c) inset}
.legend{position:absolute;z-index:500;right:10px;bottom:10px;background:rgba(15,17,20,.88);border:1px solid var(--line);border-radius:10px;padding:7px 11px;font-size:12px;color:var(--mute)}
.legend i{display:inline-block;width:70px;height:8px;border-radius:4px;vertical-align:middle;margin:0 6px;background:linear-gradient(90deg,rgba(255,90,31,.12),var(--c))}
@media(max-width:700px){.dh h1{font-size:27px}.chips{max-width:calc(100% - 20px)}.tile b{font-size:28px}}
"""


def _dep_li(i, label=None):
    """Une ligne de liste : date, lieu, texte, lien vers la source."""
    lieu = ("%s (%s)" % (i["place"], i["dep"])) if i.get("place") and i.get("dep") else (i.get("place") or "lieu non localisé")
    pub = (' · <a href="%s" rel="noopener" target="_blank">publication</a>' % esc(i["post"])) if i.get("post") and i["post"] != i.get("url") else ""
    return '<li><time datetime="%s">%s</time> — <strong>%s</strong> — %s <a href="%s" rel="noopener nofollow" target="_blank">%s</a>%s</li>' % (
        time.strftime("%Y-%m-%d", time.localtime(i["ts"])), time.strftime("%d/%m/%Y", time.localtime(i["ts"])), esc(lieu),
        esc(((i.get("age") + " ") if i.get("age") else "") + i["text"]), esc(i["url"]), esc(label or "source"), pub)


def _dep_pts(items, kind):
    """Regroupe par commune les éléments localisés : points pour la carte."""
    pts = {}
    for i in items:
        if i.get("lat") is None:
            continue
        p = pts.setdefault((i["lat"], i["lon"]), {"lat": i["lat"], "lon": i["lon"], "place": i.get("place", ""), "n": 0, "items": []})
        p["n"] += 1
        if len(p["items"]) < 6:
            p["items"].append({"d": time.strftime("%d/%m/%Y", time.localtime(i["ts"])), "t": ((i.get("age") + " · ") if i.get("age") else "") + i["text"][:200], "u": i["url"]})
    return list(pts.values())


def departement_page(slug_str):
    code = slug_str.split("-", 1)[0].upper()
    names = dep_names()
    if code not in names:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow")
    nom = names[code]
    year = YEAR
    places, arts = dep_manif(code)
    n_live = sum(1 for a in arts if a["ts"] >= time.time() - 86400)
    ps = sorted([i for i in DS["ps"]["items"] if str(i.get("dep")) == code], key=lambda i: -i["ts"])
    trav = [i for i in DS["travail"]["items"] if str(i.get("dep")) == code]
    fem = [i for i in DS["feminicides"]["items"] if str(i.get("dep")) == code]
    pol = [i for i in DS["police"]["items"] if str(i.get("dep")) == code]
    postes = sum(i.get("postes") or 0 for i in ps)
    url = dep_url(code, True)
    title = "%s (%s) : mobilisations, plans sociaux et accidents du travail en %d | ça brûle" % (nom, code, year)
    desc = ("%s (%s) : %s en 30 jours, %s en 12 mois, %s en %d. Carte et listes sourcées, mises à jour chaque jour." % (
        nom, code, pl(len(arts), "article sur des mobilisations", "articles sur des mobilisations"),
        pl(len(ps), "plan social, fermeture ou procédure d'entreprise", "plans sociaux, fermetures ou procédures d'entreprises"),
        pl(len(trav), "accident du travail mortel", "accidents du travail mortels"), year))
    lead = ("<p class=\"lead\">%s signalant une mobilisation ces 30 derniers jours (%d ces dernières 24 heures), %s de 50 salariés et plus sur 12 mois, "
            "%s, %s et %s depuis le 1er janvier %d. Cinq recensements distincts, chacun avec sa source, sur une même carte.</p>") % (
        pl(len(arts), "article", "articles"), n_live, pl(len(ps), "procédure ou annonce d'entreprise", "procédures ou annonces d'entreprises"),
        pl(len(trav), "accident du travail mortel recensé", "accidents du travail mortels recensés"),
        pl(len(fem), "féminicide conjugal", "féminicides conjugaux"), pl(len(pol), "décès lié à la police", "décès liés à la police"), year)
    counts = {"manif": len(arts), "ps": len(ps), "travail": len(trav), "fem": len(fem), "police": len(pol)}
    subs = {"manif": "dans %s · 30 jours" % pl(len(places), "commune", "communes"),
            "ps": ("%s · 12 mois" % pl(postes, "poste annoncé", "postes annoncés")) if postes else "12 mois",
            "travail": "depuis le 1er janvier", "fem": "depuis le 1er janvier", "police": "depuis le 1er janvier"}
    tiles = "".join('<a class="tile" style="--c:%s" href="#l-%s"><b>%d</b><span>%s</span><small>%s</small></a>' % (col, key, counts[key], esc(lab), esc(subs[key]))
                    for key, ds, lab, col in DEP_LAYERS)
    # recoupages : communes présentes dans au moins deux des couches liées au travail
    cross = {}
    for a in arts:
        if "travail" in (a.get("th") or []):
            cross.setdefault(a["p"], {}).setdefault("manif", []).append(a)
    for i in ps:
        if i.get("place"):
            cross.setdefault(i["place"], {}).setdefault("ps", []).append(i)
    for i in trav:
        if i.get("place"):
            cross.setdefault(i["place"], {}).setdefault("travail", []).append(i)
    crow = []
    for place, d in sorted(cross.items(), key=lambda kv: (-len(kv[1]), -sum(len(v) for v in kv[1].values()))):
        if len(d) < 2:
            continue
        parts = []
        if "manif" in d:
            parts.append('<span><i class="dot" style="--c:#ff5a1f"></i>%s</span>' % esc(pl(len(d["manif"]), "article sur une grève ou mobilisation du travail", "articles sur des grèves ou mobilisations du travail")))
        if "ps" in d:
            parts.append('<span><i class="dot" style="--c:#17c3a2"></i>%s</span>' % esc("; ".join(sorted({x["text"] for x in d["ps"]}))[:150]))
        if "travail" in d:
            parts.append('<span><i class="dot" style="--c:#f0b429"></i>%s</span>' % esc(pl(len(d["travail"]), "accident du travail mortel recensé", "accidents du travail mortels recensés")))
        crow.append("<li><b>%s</b>%s</li>" % (esc(place), "".join(parts)))
    cross_html = ""
    if crow:
        cross_html = ("<h2>Communes présentes dans plusieurs recensements</h2><p class=\"meta\">Mobilisations liées au travail, plans sociaux ou procédures d'entreprises, accidents du travail mortels : "
                      "les communes citées dans au moins deux de ces recensements. C'est une simple co-présence, sans lien de cause établi.</p><ul class=\"cross\">%s</ul>" % "".join(crow[:12]))

    qd = dep_qpv().get(code)
    if qd and qd["pop"]:
        tp = dep_pop(code)
        top = sorted(qd["qpv"], key=lambda t: -t[2])[:6]
        cross_html = (
            '<h2>Contexte : les quartiers prioritaires</h2><div class="card"><p><strong>' + frn(qd["pop"]) + " habitants</strong>"
            + ((" (" + ("%.1f" % (100.0 * qd["pop"] / tp)).replace(".", ",") + " % des " + frn(tp) + " habitants du département)") if tp else "")
            + " vivent dans l'un des <strong>" + str(len(qd["qpv"])) + " quartiers prioritaires de la politique de la ville</strong> (QPV) du département "
            "(population municipale 2024 des QPV, Insee). Les plus peuplés : "
            + ", ".join("%s (%s), %s hab." % (esc(n), esc(v.replace(",", ", ")), frn(p)) for n, v, p in top)
            + '.</p><p class="meta">Les compteurs de cette page sont des chiffres bruts, non rapportés à la population. Un QPV est la définition officielle d\'un quartier de la politique de la ville : '
            "il ne recouvre pas toutes les banlieues. Source : <a href=\"https://www.insee.fr/fr/statistiques/8210600\" rel=\"noopener\" target=\"_blank\">Insee, population des QPV 2024</a>. "
            '<a href="' + BASE_PATH + '/quartiers-prioritaires">Combien d\'argent public par habitant ?</a></p></div>'
        ) + cross_html

    def sec(key, label, col, inner, extra=""):
        return ('<details class="lst" id="l-%s" style="--c:%s"%s><summary>%s<small>%d</small></summary>%s%s</details>' % (
            key, col, " open" if key in ("manif", "ps") else "", esc(label), counts[key], extra, inner))

    top_places = sorted(places, key=lambda p: -p["n"])[:8]
    manif_inner = ('<p class="meta">Communes les plus citées : %s.</p>' % ", ".join("%s (%d)" % (esc(p["place"]), p["n"]) for p in top_places)) if top_places else ""
    manif_inner += "<ul>%s</ul>" % "".join(
        '<li><time datetime="%s">%s</time> — <strong>%s</strong> — <a href="%s" rel="noopener nofollow" target="_blank">%s</a> <span class="meta">%s</span></li>' % (
            time.strftime("%Y-%m-%d", time.localtime(a["ts"])), time.strftime("%d/%m", time.localtime(a["ts"])), esc(a["p"]), esc(a["u"]), esc(a["t"]), esc(a["s"]))
        for a in arts[:15]) if arts else "<p>Aucune mobilisation signalée dans ce département ces 30 derniers jours.</p>"
    if len(arts) > 15:
        manif_inner += '<p class="meta">Les 15 derniers articles sur %d. La carte en direct les montre tous : <a href="%s/">ouvrir la carte</a>.</p>' % (len(arts), BASE_PATH)

    def lst(items, lab, empty, limit=40):
        return ("<ul>%s</ul>" % "".join(_dep_li(i, lab) for i in items[:limit])) if items else "<p>%s</p>" % empty

    sections = (
        sec("manif", "Mobilisations signalées par la presse (30 jours)", "#ff5a1f", manif_inner)
        + sec("ps", "Plans sociaux, fermetures et procédures d'entreprises de 50 salariés et plus (12 mois)", "#17c3a2",
              lst(ps, "source", "Aucun événement recensé sur 12 mois."),
              '<p class="meta">Source : <a href="%s" target="_blank" rel="noopener">Plans sociaux et faillites en France</a> ; détail et méthode : '
              '<a href="%sdepartement/%s/" target="_blank" rel="noopener">la page de %s</a>.</p>' % (PS_BASE, PS_BASE, dep_slug(code), esc(nom)))
        + sec("travail", "Accidents du travail mortels (%d)" % year, "#f0b429", lst(trav, "article", "Aucun cas recensé depuis le début de l'année."),
              '<p class="meta">Source : <a href="%s" target="_blank" rel="noopener">compte « Accident du travail : silence, des ouvriers meurent »</a>, '
              'décompte a minima d\'après la presse. <a href="%s/accidents-du-travail">Toute la France</a>.</p>' % (DATASETS["travail"]["url"], BASE_PATH))
        + sec("fem", "Féminicides conjugaux (%d)" % year, "#9b4dff", lst(fem, "source", "Aucun cas recensé depuis le début de l'année."),
              '<p class="meta">Source : <a href="https://www.feminicides.fr/" target="_blank" rel="noopener">Collectif Féminicides par compagnons ou ex</a>. '
              'Violences conjugales : 3919 (gratuit, anonyme).</p>')
        + sec("police", "Décès liés à la police (%d)" % year, "#4b7bff", lst(pol, "source", "Aucun cas recensé depuis le début de l'année."),
              '<p class="meta">Source : <a href="https://desarmons.net/" target="_blank" rel="noopener">Désarmons-les</a>.</p>'))

    data = {"feature": dep_geo()[code],
            "layers": {"manif": _dep_pts([{"ts": a["ts"], "lat": p["lat"], "lon": p["lon"], "place": p["place"], "text": a["t"], "url": a["u"]}
                                          for p in places for a in p["items"] if p["lat"] is not None], "manif"),
                       "ps": _dep_pts(ps, "ps"), "travail": _dep_pts(trav, "travail"), "fem": _dep_pts(fem, "fem"), "police": _dep_pts(pol, "police")},
            "cols": {k: c for k, _, _, c in DEP_LAYERS}, "labs": {k: l for k, _, l, _ in DEP_LAYERS}, "counts": counts}
    js = """(function(){
var D=JSON.parse(document.getElementById('dd').textContent);
var map=L.map('dmap',{zoomControl:false,scrollWheelZoom:false,tap:false,minZoom:5,maxZoom:14});L.control.zoom({position:'bottomright'}).addTo(map);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:14,attribution:'\\u00a9 OpenStreetMap',className:'dark-tiles'}).addTo(map);
map.on('focus',function(){map.scrollWheelZoom.enable()});map.on('blur',function(){map.scrollWheelZoom.disable()});
var g=D.feature.geometry,polys=g.type==='Polygon'?[g.coordinates]:g.coordinates,holes=[];
polys.forEach(function(p){holes.push(p[0].map(function(c){return [c[1],c[0]]}))});
var gj=L.geoJSON(D.feature,{style:{color:'#ff5a1f',weight:2.5,fillColor:'#ff5a1f',fillOpacity:.07}});
map.fitBounds(gj.getBounds(),{padding:[24,24]});gj.addTo(map);
L.polygon([[[-85,-180],[-85,180],[85,180],[85,-180]]].concat(holes),{stroke:false,fillColor:'#07080a',fillOpacity:.68,interactive:false}).addTo(map);
var groups={},chips=document.getElementById('chips');
function popup(p,col){var el=document.createElement('div'),b=document.createElement('b');b.textContent=p.place+(p.n>1?' \\u00b7 '+p.n:'');el.appendChild(b);
p.items.forEach(function(i){var q=document.createElement('p'),a=document.createElement('a');a.href=i.u;a.target='_blank';a.rel='noopener nofollow';a.textContent=i.t.slice(0,150)+(i.t.length>150?'\\u2026':'');
if(i.d){var s=document.createElement('small');s.style.color='#8b929c';s.textContent=i.d+' \\u00b7 ';q.appendChild(s)}q.appendChild(a);el.appendChild(q)});return el}
Object.keys(D.layers).forEach(function(k){
 var col=D.cols[k],lg=L.layerGroup().addTo(map);groups[k]=lg;
 D.layers[k].forEach(function(p){var s=Math.round(Math.min(46,15+p.n*(k==='manif'?4:9)));
  L.marker([p.lat,p.lon],{icon:L.divIcon({className:'',iconSize:[s,s],iconAnchor:[s/2,s/2],html:'<div class="pm" style="--c:'+col+';width:'+s+'px;height:'+s+'px"></div>'}),keyboard:false})
   .bindPopup(popup(p,col),{maxWidth:Math.min(300,innerWidth-60)}).addTo(lg)});
 var b=document.createElement('button');b.type='button';b.className='chip';b.style.setProperty('--c',col);
 b.innerHTML='<i></i>'+D.labs[k]+' <b>'+D.counts[k]+'</b>';
 b.onclick=function(){var off=b.classList.toggle('off');off?map.removeLayer(lg):map.addLayer(lg)};chips.appendChild(b)});
})();"""
    body = "<style>" + DEP_CSS + "</style>" + (
            '<link rel="stylesheet" href="@@B@@/lib/leaflet.css"><script src="@@B@@/lib/leaflet.js"></script>'
            '<p class="meta"><a href="@@B@@/departements">Tous les départements</a> › %s</p>'
            '<div class="dh"><h1>%s <small>%s</small></h1>%s</div><div class="tiles">%s</div>'
            '<div class="mapcard"><div id="dmap" aria-label="Carte du département %s : mobilisations, plans sociaux, accidents du travail, féminicides, décès police"></div>'
            '<div class="chips" id="chips"></div></div>'
            '<p class="meta">Les positions sont ramenées au centre de la commune ; les noms de victimes ne sont jamais affichés. Chaque couche a sa propre source et ses propres limites '
            "(décomptes a minima, d'après la presse ou des collectifs) : les voir côte à côte ne dit rien d'un lien de cause. Voir <a href=\"@@B@@/sources\">Sources et méthode</a>.</p>"
            '%s%s<script id="dd" type="application/json">%s</script><script>%s</script>') % (
        esc(code), esc(nom), esc(code), lead, tiles, esc(nom), cross_html, sections,
        json.dumps(data, ensure_ascii=False).replace("</", "<\\/"), js)
    body = body.replace("@@B@@", BASE_PATH)
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title.split(" | ")[0], "url": url, "inLanguage": "fr-FR",
          "description": desc, "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"},
          "about": {"@type": "AdministrativeArea", "name": nom},
          "breadcrumb": {"@type": "BreadcrumbList", "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": "ça brûle", "item": BASE_URL + "/"},
              {"@type": "ListItem", "position": 2, "name": "Départements", "item": BASE_URL + "/departements"},
              {"@type": "ListItem", "position": 3, "name": "%s (%s)" % (nom, code), "item": url}]}}
    return 200, shell(title, desc, url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


def departements_page():
    st = dep_stats()
    names = dep_names()
    url = BASE_URL + "/departements"
    tot = {k: sum(v[k] for v in st.values()) for k in ("manif", "ps", "travail", "fem", "police")}
    title = "Mobilisations, plans sociaux et accidents du travail par département | ça brûle"
    desc = ("Carte des départements français : mobilisations signalées par la presse, plans sociaux et faillites d'entreprises, accidents du travail mortels, "
            "féminicides conjugaux et décès liés à la police. Une fiche sourcée par département, mise à jour chaque jour.")
    metrics = [(k, lab, col) for k, _, lab, col in DEP_LAYERS] + [("qpv", "Part de la population en quartiers prioritaires", "#38bdf8")]
    chips = "".join('<button type="button" class="chip%s" data-k="%s" style="--c:%s"><i></i>%s</button>' % (" on" if k == "manif" else "", k, col, esc(lab))
                    for k, lab, col in metrics)
    order = sorted(names, key=lambda c: (c.zfill(3) if c.isdigit() else c))
    rows = "".join('<tr><td><a href="%s">%s</a> <span class="meta">%s</span></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
        dep_url(c), esc(names[c]), esc(c), *[(str(st[c][k]) if st[c][k] else '<span class="z">·</span>') for k in ("manif", "ps", "travail", "fem", "police")],
        (frn(st[c]["qpvpop"]) + " (" + str(st[c]["qpv"]).replace(".", ",") + " %)") if st[c]["qpvpop"] else '<span class="z">·</span>') for c in order)
    data = {"stats": {c: dict(st[c], u=dep_url(c), n=names[c]) for c in names}, "cols": {k: col for k, _, _, col in DEP_LAYERS},
            "labs": {k: lab for k, _, lab, _ in DEP_LAYERS}, "units": {"manif": "articles (30 j)", "ps": "annonces (12 mois)", "travail": "cas (%d)" % YEAR,
                                                                      "fem": "cas (%d)" % YEAR, "police": "cas (%d)" % YEAR,
                                                                      "qpv": "% de la population en QPV"}}
    data["cols"]["qpv"], data["labs"]["qpv"] = "#38bdf8", "Part de la population en quartiers prioritaires"
    js = """(function(){
var D=JSON.parse(document.getElementById('dd').textContent),cur='manif';
var map=L.map('fmap',{zoomControl:true,scrollWheelZoom:false,tap:false,minZoom:5,maxZoom:9,zoomSnap:.25}).fitBounds([[41.2,-5.4],[51.2,9.8]]);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:9,attribution:'\\u00a9 OpenStreetMap',className:'dark-tiles'}).addTo(map);
map.on('focus',function(){map.scrollWheelZoom.enable()});map.on('blur',function(){map.scrollWheelZoom.disable()});
var layer=null,legend=document.getElementById('legend');
function mx(){var m=1;for(var c in D.stats){if(D.stats[c][cur]>m)m=D.stats[c][cur]}return m}
function paint(){var m=mx(),col=D.cols[cur];
 if(!layer)return;layer.eachLayer(function(l){var v=D.stats[l.feature.properties.code][cur]||0;
  l.setStyle({fillColor:col,fillOpacity:v?.14+.78*Math.sqrt(v/m):.03,color:v?col:'#2a2f37',weight:v?1:.6,opacity:.9});
  l.setTooltipContent('<b>'+D.stats[l.feature.properties.code].n+'</b><br>'+v+' '+D.units[cur])});
 legend.style.setProperty('--c',col);legend.innerHTML='0<i></i>'+m+' \\u00b7 '+D.units[cur]}
fetch('@@B@@/lib/departements.geojson').then(function(r){return r.json()}).then(function(g){
 layer=L.geoJSON(g,{style:{weight:.6,color:'#2a2f37',fillOpacity:.03},onEachFeature:function(f,l){
  l.bindTooltip('',{sticky:true});l.on('click',function(){location.href=D.stats[f.properties.code].u});
  l.on('mouseover',function(){l.setStyle({weight:2.5})});l.on('mouseout',function(){paint()})}}).addTo(map);paint()});
document.querySelectorAll('.mt .chip').forEach(function(b){b.onclick=function(){cur=b.getAttribute('data-k');
 document.querySelectorAll('.mt .chip').forEach(function(x){x.classList.toggle('on',x===b)});paint()}});
})();""".replace("@@B@@", BASE_PATH)
    body = "<style>" + DEP_CSS + "</style>" + (
            '<link rel="stylesheet" href="@@B@@/lib/leaflet.css"><script src="@@B@@/lib/leaflet.js"></script>'
            "<h1>Mobilisations, plans sociaux et accidents du travail, département par département</h1>"
            '<p class="lead">%d articles sur des mobilisations en 30 jours, %d annonces de plans sociaux, fermetures et procédures d\'entreprises de 50 salariés et plus sur 12 mois, '
            "%d accidents du travail mortels, %d féminicides conjugaux et %d décès liés à la police depuis le 1er janvier %d. Choisissez un recensement, cliquez un département.</p>"
            '<div class="mt">%s</div><div class="mapcard"><div id="fmap" aria-label="Carte de France par département"></div><div class="legend" id="legend"></div></div>'
            '<p class="meta">Chiffres bruts, non rapportés à la population : un département très peuplé ressortira davantage. Les mobilisations dépendent de la presse locale couvrant le département, '
            "les décomptes sont a minima. Détail des sources : <a href=\"@@B@@/sources\">Sources et méthode</a>.</p>"
            '<h2>Tous les départements</h2><table class="dtab"><thead><tr><th>Département</th><th>Mobilisations</th><th>Plans sociaux</th><th>Accidents du travail</th>'
            "<th>Féminicides</th><th>Décès police</th><th>Habitants en QPV</th></tr></thead><tbody>%s</tbody></table>"
            '<script id="dd" type="application/json">%s</script><script>%s</script>') % (
        tot["manif"], tot["ps"], tot["travail"], tot["fem"], tot["police"], YEAR, chips, rows,
        json.dumps(data, ensure_ascii=False).replace("</", "<\\/"), js)
    body = body.replace("@@B@@", BASE_PATH)
    ld = {"@context": "https://schema.org", "@type": "CollectionPage", "name": title.split(" | ")[0], "url": url, "inLanguage": "fr-FR",
          "description": desc, "isPartOf": {"@id": BASE_URL + "/#site"}, "dateModified": iso_now(),
          "hasPart": [{"@type": "WebPage", "name": names[c], "url": dep_url(c, True)} for c in order]}
    return 200, shell(title, desc, url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


# ---------------------------------------------------------------------------
# Quartiers prioritaires et argent public : dotations de l'État par habitant selon la part de population en QPV
# Les chiffres viennent de qpv_dotations.json, produit par qpv-work/build_dotations.py (fichier DGCL « critères de répartition »).
# ---------------------------------------------------------------------------
QPVD = {"mtime": 0, "d": None}
QPV_GROUPES = [("aucun", "Aucun quartier prioritaire"), ("moins10", "Moins de 10 % des habitants en QPV"),
               ("10a25", "10 à 25 % des habitants en QPV"), ("25plus", "25 % et plus des habitants en QPV")]


def qpv_data():
    path = os.path.join(HERE, "qpv_dotations.json")
    try:
        m = os.path.getmtime(path)
        if m != QPVD["mtime"]:
            QPVD["d"], QPVD["mtime"] = json.load(open(path, encoding="utf-8")), m
    except Exception as ex:
        print("qpv_dotations :", ex)
    return QPVD["d"]


def _eur(x):
    return frn(round(x)) + "\u202f\u20ac"


def qpv_page():
    d = qpv_data()
    url = BASE_URL + "/quartiers-prioritaires"
    if not d:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow")
    an = d["annee"]
    title = "Quartiers prioritaires : combien d'argent public par habitant ? Les chiffres ouverts | ça brûle"
    g10 = d["groupes"]["10000"]
    fmt = lambda x: ("%.1f" % x).replace(".", ",")
    rh = g10["25plus"]["total_hab"] / g10["aucun"]["total_hab"]
    rp = d["besoins"]["groupes"]["10000"]["25plus"]["total_par_pauvre"] / d["besoins"]["groupes"]["10000"]["aucun"]["total_par_pauvre"]
    desc = ("Les communes où au moins un quart des habitants vivent en quartier prioritaire reçoivent %s par habitant de dotations de l'État (DSU, DSR, forfaitaire, %d), "
            "contre %s pour les communes comparables sans quartier prioritaire, soit %s fois plus. Rapporté au nombre de personnes pauvres, l'écart tombe à %s fois. "
            "Ce que disent les données ouvertes, et ce qu'elles ne disent pas.") % (
        _eur(g10["25plus"]["total_hab"]), an, _eur(g10["aucun"]["total_hab"]), fmt(rh), fmt(rp))

    def bars(key):
        gs = d["groupes"][key]
        mx = max(g["total_hab"] for g in gs.values())
        out = []
        for gid, lab in QPV_GROUPES:
            g = gs.get(gid)
            if not g:
                continue
            seg = lambda v, c, n: ('<i style="width:%.2f%%;background:%s" title="%s : %s par habitant"></i>' % (100.0 * v / mx, c, n, _eur(v))) if v > 0 else ""
            out.append('<div class="br"><div class="bl"><b>%s</b><span class="meta">%s communes, %s habitants</span></div><div class="bb">%s%s%s</div><div class="bv">%s</div></div>' % (
                esc(lab), frn(g["communes"]), frn(g["pop"]), seg(g["dsu_hab"], "#ff5a1f", "DSU"), seg(g["dsr_hab"], "#17c3a2", "DSR"),
                seg(g["forf_hab"], "#6b7480", "Dotation forfaitaire"), "<b>%s</b>" % _eur(g["total_hab"])))
        rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(lab), frn(gs[gid]["communes"]), frn(gs[gid]["pop_qpv"]), _eur(gs[gid]["dsu_hab"]), _eur(gs[gid]["forf_hab"] + gs[gid]["dsr_hab"]), _eur(gs[gid]["total_hab"]))
            for gid, lab in QPV_GROUPES if gid in gs)
        return ('<div class="bars" data-k="%s">%s<p class="leg"><i style="background:#ff5a1f"></i>DSU (dotation de solidarité urbaine) <i style="background:#17c3a2"></i>DSR (solidarité rurale) '
                '<i style="background:#6b7480"></i>Dotation forfaitaire</p>'
                '<table class="dtab"><thead><tr><th>Groupe de communes</th><th>Communes</th><th>Habitants en QPV</th><th>DSU par hab.</th><th>Forfaitaire + DSR par hab.</th><th>Total par hab.</th></tr></thead>'
                "<tbody>%s</tbody></table></div>") % (key, "".join(out), rows)

    def needs(key):
        gs = d["besoins"]["groupes"][key]
        mx = max(g["total_par_pauvre"] for g in gs.values())
        out = []
        for gid, lab in QPV_GROUPES:
            g = gs.get(gid)
            if not g:
                continue
            out.append('<div class="br"><div class="bl"><b>%s</b><span class="meta">%s %% de pauvreté · %s communes</span></div><div class="bb">'
                       '<i style="width:%.2f%%;background:#ff5a1f" title="DSU : %s par personne pauvre"></i>'
                       '<i style="width:%.2f%%;background:#6b7480" title="Autres dotations : %s par personne pauvre"></i></div><div class="bv"><b>%s</b></div></div>' % (
                esc(lab), str(g["taux_pauvrete"]).replace(".", ","), frn(g["communes"]), 100.0 * g["dsu_par_pauvre"] / mx, _eur(g["dsu_par_pauvre"]),
                100.0 * (g["total_par_pauvre"] - g["dsu_par_pauvre"]) / mx, _eur(g["total_par_pauvre"] - g["dsu_par_pauvre"]), _eur(g["total_par_pauvre"])))
        rows = "".join("<tr><td>%s</td><td>%s</td><td>%s %%</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(lab), frn(gs[gid]["communes"]), str(gs[gid]["taux_pauvrete"]).replace(".", ","), str(gs[gid]["rsa_pa_100"]).replace(".", ","),
            _eur(gs[gid]["dsu_par_pauvre"]), _eur(gs[gid]["total_par_pauvre"])) for gid, lab in QPV_GROUPES if gid in gs)
        return ('<div class="bars" data-k="%s">%s<p class="leg"><i style="background:#ff5a1f"></i>DSU <i style="background:#6b7480"></i>Autres dotations (forfaitaire, DSR)</p>'
                '<table class="dtab"><thead><tr><th>Groupe de communes</th><th>Communes</th><th>Taux de pauvreté</th><th>RSA et prime d\'activité (personnes pour 100 hab.)</th>'
                "<th>DSU par personne pauvre</th><th>Total par personne pauvre</th></tr></thead><tbody>%s</tbody></table></div>") % (key, "".join(out), rows)

    nb10 = d["besoins"]["groupes"]["10000"]
    inv_html, ri, rip = "", None, None
    ivd = d.get("invest")
    if ivd:
        i10, iall = ivd["groupes"]["10000"], ivd["groupes"]["toutes"]
        ri = i10["25plus"]["total_hab_an"] / i10["aucun"]["total_hab_an"]
        rip = i10["25plus"]["total_par_pauvre_an"] / i10["aucun"]["total_par_pauvre_an"]
        y0, y1 = ivd["periode"]

        def inv(key):
            gs = ivd["groupes"][key]
            mx = max(g["total_hab_an"] for g in gs.values())
            out = []
            for gid, lab in QPV_GROUPES:
                g = gs.get(gid)
                if not g:
                    continue
                seg = lambda v, c, n: ('<i style="width:%.2f%%;background:%s" title="%s : %s par habitant et par an"></i>' % (100.0 * v / mx, c, n, ("%.1f" % v).replace(".", ",") + " \u20ac")) if v > 0 else ""
                out.append('<div class="br"><div class="bl"><b>%s</b><span class="meta">%s communes, %s projets</span></div><div class="bb">%s%s%s</div><div class="bv"><b>%s</b></div></div>' % (
                    esc(lab), frn(g["communes"]), frn(g["projets"]), seg(g["dpv_hab_an"], "#ff5a1f", "DPV"), seg(g["dsil_hab_an"], "#9b6bff", "DSIL"),
                    seg(g["detr_hab_an"], "#17c3a2", "DETR"), ("%.1f" % g["total_hab_an"]).replace(".", ",") + "\u202f\u20ac"))
            rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(lab), frn(gs[gid]["communes"]), *[("%.1f" % gs[gid][k]).replace(".", ",") for k in ("detr_hab_an", "dsil_hab_an", "dpv_hab_an", "total_hab_an")],
                ("%.0f" % gs[gid]["total_par_pauvre_an"]).replace(".", ",") if gs[gid]["total_par_pauvre_an"] is not None else "n.d.")
                for gid, lab in QPV_GROUPES if gid in gs)
            return ('<div class="bars" data-k="%s">%s<p class="leg"><i style="background:#ff5a1f"></i>DPV (politique de la ville) <i style="background:#9b6bff"></i>DSIL '
                    '<i style="background:#17c3a2"></i>DETR (surtout rural)</p>'
                    '<table class="dtab"><thead><tr><th>Groupe de communes</th><th>Communes</th><th>DETR par hab. et par an</th><th>DSIL</th><th>DPV</th><th>Total par hab. et par an</th>'
                    "<th>Total par personne pauvre et par an</th></tr></thead><tbody>%s</tbody></table></div>") % (key, "".join(out), rows)

        inv_html = (
            "<h2>L'investissement : subventions de l'\u00c9tat aux projets des communes</h2>"
            "<p>L'\u00c9tat finance chaque ann\u00e9e des projets d'investissement des communes par trois dotations : la <strong>DPV</strong> (dotation politique de la ville, r\u00e9serv\u00e9e aux communes \u00e0 quartiers prioritaires), "
            "la <strong>DETR</strong> (surtout les communes rurales) et la <strong>DSIL</strong>. En moyenne " + str(y0) + "-" + str(y1) + ", les communes de 10\u202f000 habitants et plus o\u00f9 25 % ou plus des habitants vivent en QPV "
            "ont re\u00e7u <strong>" + ("%.1f" % i10["25plus"]["total_hab_an"]).replace(".", ",") + " \u20ac par habitant et par an</strong>, dont " + ("%.1f" % i10["25plus"]["dpv_hab_an"]).replace(".", ",")
            + " de DPV, contre " + ("%.1f" % i10["aucun"]["total_hab_an"]).replace(".", ",") + " \u20ac dans celles sans QPV (" + fmt(ri) + " fois plus). Rapport\u00e9 \u00e0 la pauvret\u00e9 : "
            + ("%.0f" % i10["25plus"]["total_par_pauvre_an"]) + " \u20ac contre " + ("%.0f" % i10["aucun"]["total_par_pauvre_an"]) + " \u20ac par personne pauvre et par an (" + fmt(rip) + " fois). "
            "En prenant toutes les communes, y compris les petites, celles sans QPV re\u00e7oivent " + ("%.1f" % iall["aucun"]["total_hab_an"]).replace(".", ",") + " \u20ac par habitant et par an, dont "
            + ("%.1f" % iall["aucun"]["detr_hab_an"]).replace(".", ",") + " de DETR, contre " + ("%.1f" % iall["25plus"]["total_hab_an"]).replace(".", ",") + " \u20ac pour les communes \u00e0 forte part de QPV.</p>"
            + inv("10000") + inv("toutes").replace('class="bars"', 'class="bars" style="display:none"', 1)
            + '<p class="meta">Seuls les projets port\u00e9s par des communes sont compt\u00e9s : ceux des intercommunalit\u00e9s (nombreux en milieu rural), des syndicats et des d\u00e9partements ne peuvent pas \u00eatre rattach\u00e9s '
            "\u00e0 une commune. Ce sont des subventions de l'\u00c9tat d\u00e9cid\u00e9es projet par projet : ni les financements de l'ANRU, ni ceux des collectivit\u00e9s, ni le droit commun n'en font partie. "
            "Euros courants, moyenne annuelle sur " + str(y1 - y0 + 1) + " ans, population " + str(ivd["population_annee"]) + ". Source : "
            '<a href="https://www.data.gouv.fr/datasets/projets-finances-par-les-dotations-de-soutien-a-linvestissement-des-collectivites-territoriales" rel="noopener" target="_blank">DGCL, projets financ\u00e9s par les dotations d\'investissement</a>.</p>')
    eq_html, eq_sentence = "", ""
    eqd = d.get("equip")
    if eqd:
        ec, ee = eqd["communes"]["10000"], eqd["ensembles"]
        e0, e1 = eqd["periode"]
        r_eh = ec["25plus"]["eq_hab_an"] / ec["aucun"]["eq_hab_an"]
        r_ep = ec["aucun"]["eq_par_pauvre_an"] / ec["25plus"]["eq_par_pauvre_an"]
        eq_sentence = ("Côté dépenses d'équipement des collectivités locales, tous financements confondus, les communes de 10 000 habitants et plus à forte part de QPV dépensent " + fmt(r_eh)
                       + " fois ce que dépensent celles sans QPV par habitant, et " + fmt(1 / r_ep) + " fois par personne pauvre. ")

        def eqrows(gs, label):
            return "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(lab), frn(gs[gid]["n"]), _eur(gs[gid]["eq_hab_an"]), _eur(gs[gid]["eq_par_pauvre_an"]) if gs[gid]["eq_par_pauvre_an"] else "n.d.")
                for gid, lab in QPV_GROUPES if gid in gs)

        head = "<thead><tr><th>%s</th><th>%s</th><th>Équipement par hab. et par an</th><th>Par personne pauvre et par an</th></tr></thead>"
        eq_html = (
            "<h2>Ce que les collectivités dépensent en équipement</h2>"
            "<p>Au-delà des subventions de l'État, que dépensent les collectivités en équipement (voirie, bâtiments, écoles, équipements sportifs, réseaux), tous financements confondus : "
            "ressources propres, emprunts et subventions ? En moyenne " + str(e0) + "-" + str(e1) + ", les communes de 10 000 habitants et plus où 25 % ou plus des habitants vivent en QPV ont dépensé <strong>"
            + _eur(ec["25plus"]["eq_hab_an"]) + " par habitant et par an</strong>, contre " + _eur(ec["aucun"]["eq_hab_an"]) + " dans celles sans QPV. Rapporté au nombre de personnes pauvres : "
            + _eur(ec["25plus"]["eq_par_pauvre_an"]) + " contre " + _eur(ec["aucun"]["eq_par_pauvre_an"]) + " (" + fmt(r_ep) + " fois moins).</p>"
            '<table class="dtab">' + head % ("Communes de 10\u202f000 habitants et plus", "Communes") + "<tbody>" + eqrows(ec, "") + "</tbody></table>"
            "<p>Dans les métropoles et les communautés, une partie des équipements est portée par l'intercommunalité. En ajoutant ses dépenses à celles des communes (ensembles intercommunaux, "
            "classés selon la part de QPV dans toute leur population) :</p>"
            '<table class="dtab">' + head % ("Ensembles intercommunaux (communes + intercommunalité)", "Ensembles") + "<tbody>" + eqrows(ee, "") + "</tbody></table>"
            '<p class="meta">Sources : <a href="https://data.ofgl.fr/explore/dataset/ofgl-base-communes/" rel="noopener" target="_blank">OFGL, comptes des communes</a> et '
            '<a href="https://data.ofgl.fr/explore/dataset/ofgl-base-ei/" rel="noopener" target="_blank">comptes consolidés des ensembles intercommunaux</a> (DGFiP), budgets principaux. '
            "Ne sont pas comptés : l'investissement des bailleurs sociaux (où passe une grande part de la rénovation urbaine), des départements, des régions et de l'État. "
            "Les communes à forte part de QPV ont aussi moins de ressources propres : une dépense plus faible par personne pauvre peut venir de cette contrainte autant que d'un choix. "
            "Il y a peu d'ensembles intercommunaux dont la part de QPV atteint 25 % : lire ce groupe avec prudence. Euros courants.</p>")
    fo_html, fo_sentence = "", ""
    fod = d.get("fonct")
    if fod:
        fg = fod["groupes"]
        f0, f1 = fod["periode"]
        ga, gq = fg["aucun"], fg["25plus"]
        tot = "Dépenses de fonctionnement"
        gap = gq["hab"][tot] - ga["hab"][tot]
        pers = gq["hab"]["Frais de personnel"] - ga["hab"]["Frais de personnel"]
        conc = gq["hab"]["Concours de l'Etat"] - ga["hab"]["Concours de l'Etat"]
        imp = gq["hab"]["Impôts locaux"] - ga["hab"]["Impôts locaux"]
        vte = gq["hab"]["Ventes de biens et services"] - ga["hab"]["Ventes de biens et services"]
        rpp = ga["pauvre"][tot] / gq["pauvre"][tot]
        sg = lambda v: ("+" if v >= 0 else "\u2212") + _eur(abs(v))
        fo_lines = [("Recettes", None), ("Impôts locaux", "Impôts locaux"), ("Concours de l'État (DGF et autres)", "Concours de l'Etat"),
                    ("Ventes de biens et services (cantine, crèche, piscine…)", "Ventes de biens et services"), ("Total des recettes de fonctionnement", "Recettes de fonctionnement"),
                    ("Dépenses", None), ("Frais de personnel", "Frais de personnel"), ("Achats et charges externes", "Achats et charges externes"),
                    ("Dépenses d'intervention (subventions et transferts versés)", "Dépenses d'intervention"), ("Charges financières (intérêts)", "Charges financières"),
                    ("Autres dépenses", "Autres dépenses de fonctionnement"), ("Total des dépenses de fonctionnement", tot),
                    ("Bilan", None), ("Épargne brute", "Epargne brute"), ("Encours de dette (au 31 décembre)", "Encours de dette")]
        fo_rows = []
        for lab, k in fo_lines:
            if k is None:
                fo_rows.append('<tr><td colspan="6"><b>%s</b></td></tr>' % esc(lab))
                continue
            fo_rows.append("<tr><td>%s</td>%s<td>%s</td></tr>" % (esc(lab), "".join("<td>%s</td>" % _eur(fg[g]["hab"][k]) for g in ("aucun", "moins10", "10a25", "25plus")),
                                                                  sg(gq["hab"][k] - ga["hab"][k])))
        fo_sentence = ("Côté fonctionnement des communes, celles à forte part de QPV dépensent " + sg(gap).lstrip("+") + " de plus par habitant que celles sans QPV, dont " + _eur(pers)
                       + " de frais de personnel ; elles reçoivent " + _eur(conc) + " de plus de l'État, " + _eur(abs(imp)) + (" de plus" if imp >= 0 else " de moins") + " d'impôts locaux et "
                       + _eur(abs(vte)) + (" de plus" if vte >= 0 else " de moins") + " de ventes de biens et services. ")
        fo_html = (
            "<h2>Où va l'argent : le fonctionnement des communes</h2>"
            "<p>Les dotations de l'État ne financent pas d'équipements en particulier : elles entrent dans le budget courant des communes, avec les impôts locaux et les tarifs. "
            "Pour voir ce que les communes en font, voici leurs recettes et dépenses de fonctionnement par habitant (communes de 10 000 habitants et plus, moyenne " + str(f0) + "-" + str(f1) + ", budgets principaux). "
            "Les communes où 25 % ou plus des habitants vivent en QPV dépensent <strong>" + _eur(gq["hab"][tot]) + "</strong> par habitant et par an, contre <strong>" + _eur(ga["hab"][tot]) + "</strong> sans QPV, soit "
            + _eur(gap) + " de plus. Les frais de personnel (l'ensemble des agents de la commune) représentent " + _eur(pers) + " de cet écart, soit "
            + str(int(round(100.0 * pers / gap))) + " %. Du côté des recettes, les concours de l'État sont de " + _eur(gq["hab"]["Concours de l'Etat"]) + " par habitant contre " + _eur(ga["hab"]["Concours de l'Etat"])
            + " (" + sg(conc) + "), alors que les impôts locaux sont proches (" + sg(imp) + ") et que les ventes de biens et services sont " + ("plus faibles" if vte < 0 else "plus élevées") + " (" + sg(vte) + "). "
            "L'épargne brute (recettes moins dépenses de fonctionnement) est du même ordre dans les deux groupes (" + _eur(gq["hab"]["Epargne brute"]) + " et " + _eur(ga["hab"]["Epargne brute"])
            + ") : les recettes supplémentaires sont dépensées. L'encours de dette est plus élevé (" + _eur(gq["hab"]["Encours de dette"]) + " par habitant contre " + _eur(ga["hab"]["Encours de dette"]) + "). "
            "Rapportée au nombre de personnes pauvres, leur dépense de fonctionnement est de " + _eur(gq["pauvre"][tot]) + " contre " + _eur(ga["pauvre"][tot]) + " (" + fmt(rpp) + " fois moins).</p>"
            '<table class="dtab"><thead><tr><th>Par habitant et par an</th><th>Sans QPV</th><th>Moins de 10 % en QPV</th><th>10 à 25 %</th><th>25 % et plus</th><th>Écart (25 % et plus contre sans QPV)</th></tr></thead><tbody>'
            + "".join(fo_rows) + "</tbody></table>"
            '<p class="meta">Source : <a href="https://data.ofgl.fr/explore/dataset/ofgl-base-communes/" rel="noopener" target="_blank">OFGL, comptes des communes</a> (DGFiP), budgets principaux, euros courants. '
            "La nomenclature par nature ne dit pas à quoi servent les dépenses : on ne voit pas ce qui va à l'école, à l'action sociale ou à la sécurité, car la ventilation par fonction n'est pas publiée par l'OFGL pour les communes. "
            "L'argent est fongible : on ne peut pas dire qu'une dotation précise paie une dépense précise. Les budgets annexes et les centres communaux d'action sociale (CCAS) ne sont pas comptés. "
            "Ces chiffres ne disent pas si les besoins locaux sont couverts, ni ce que ferait une commune avec d'autres ressources.</p>")
    bpe_html, bpe_sentence = "", ""
    bd = d.get("bpe")
    if bd:
        bg = bd["groupes"]["10000"]
        lab_of = {t["cle"]: t["lib"] for t in bd["themes"]}
        pct = lambda r: ("%+.0f" % ((r - 1) * 100)).replace("-", "\u2212") + " %"
        rat = {t["cle"]: (bg["25plus"][t["cle"]] / bg["aucun"][t["cle"]]) for t in bd["themes"] if bg["aucun"][t["cle"]] >= 1.0}
        low = sorted(rat, key=rat.get)[:3]
        high = sorted(rat, key=rat.get, reverse=True)[:3]
        rows, dom = [], None
        for t in bd["themes"]:
            if t["dom"] != dom:
                dom = t["dom"]
                rows.append('<tr><td colspan="6"><b>%s</b></td></tr>' % esc(dom))
            k = t["cle"]
            ecart = pct(bg["25plus"][k] / bg["aucun"][k]) if bg["aucun"][k] >= 1.0 else "n.s."
            rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(t["lib"]), *[("%.1f" % bg[g][k]).replace(".", ",") for g in ("aucun", "moins10", "10a25", "25plus")], ecart))
        bpe_sentence = ("Côté accès aux services (équipements pour 10 000 habitants), la plupart des écarts sont modestes et vont dans les deux sens. Moins : "
                        + ", ".join("%s (%s)" % (lab_of[k].lower(), pct(rat[k])) for k in low) + " ; plus : "
                        + ", ".join("%s (%s)" % (lab_of[k].lower(), pct(rat[k])) for k in high) + ". ")
        bpe_html = (
            "<h2>L'accès aux services : combien d'équipements pour 10 000 habitants ?</h2>"
            "<p>Les euros ne disent pas ce que les habitants trouvent sur place. La base permanente des équipements de l'Insee recense, commune par commune, les services accessibles au public. "
            "Pour 10 000 habitants, dans les communes de 10 000 habitants et plus :</p>"
            '<table class="dtab"><thead><tr><th>Équipement</th><th>Sans QPV</th><th>Moins de 10 % en QPV</th><th>10 à 25 %</th><th>25 % et plus</th><th>Écart (25 % et plus contre sans QPV)</th></tr></thead><tbody>'
            + "".join(rows) + "</tbody></table>"
            '<p class="meta">Source : <a href="https://www.insee.fr/fr/metadonnees/source/operation/s2216/presentation" rel="noopener" target="_blank">Insee, base permanente des équipements</a> au 1er janvier ' + str(bd["annee"]) + ". "
            "Les équipements sont rattachés à la commune, pas au quartier : un équipement peut se trouver hors du QPV. Un nombre ne dit rien de la taille, des horaires, des délais d'attente ni de la qualité du service. "
            "« n.s. » : trop peu d'équipements pour comparer. Le nombre d'équipements par habitant n'est pas non plus le nombre d'équipements accessibles à pied ou en transport.</p>")
    qs_html, qs_sentence = serv_table_html()
    evo = ""
    prec = d.get("precedent")
    if prec and prec.get("besoins") and prec["groupes"].get("10000"):
        p10, pb10 = prec["groupes"]["10000"], prec["besoins"]["10000"]
        erows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(lab), _eur(p10[gid]["dsu_hab"]), _eur(g10[gid]["dsu_hab"]), _eur(p10[gid]["total_hab"]), _eur(g10[gid]["total_hab"]),
            _eur(pb10[gid]["total_par_pauvre"]), _eur(nb10[gid]["total_par_pauvre"])) for gid, lab in QPV_GROUPES if gid in p10 and gid in g10)
        evo = ('<h2>Évolution depuis ' + str(prec["annee"]) + '</h2><table class="dtab"><thead><tr><th>Communes de 10\u202f000 habitants et plus</th>'
               '<th>DSU par hab. ' + str(prec["annee"]) + '</th><th>' + str(an) + '</th><th>Total par hab. ' + str(prec["annee"]) + '</th><th>' + str(an) + '</th>'
               '<th>Total par personne pauvre ' + str(prec["annee"]) + '</th><th>' + str(an) + '</th></tr></thead><tbody>' + erows + '</tbody></table>'
               '<p class="meta">Euros courants, sans correction de l\'inflation. Pour la pauvreté, c\'est le même millésime Filosofi dans les deux cas.</p>')
    top = "".join('<tr><td><a href="%s/ville/%s-%s">%s</a> <span class="meta">%s</span></td><td>%s</td><td>%s %%</td><td>%s</td><td>%s</td></tr>' % (
        BASE_PATH, esc(norm(r["nom"]).replace(" ", "-")), esc(r["code"]), esc(r["nom"]), esc(r["dep"]), frn(r["pop"]), str(r["part_qpv"]).replace(".", ","),
        _eur(r["dsu_hab"]), _eur(r["total_hab"])) for r in d["top"])
    fr_ = d["france"]
    css = ("<style>" + DEP_CSS + """
.br{display:grid;grid-template-columns:minmax(150px,230px) 1fr 70px;gap:12px;align-items:center;margin:12px 0}
.bl b{display:block;font-size:14px}.bb{display:flex;height:26px;background:#10131a;border:1px solid var(--line);border-radius:7px;overflow:hidden}
.bb i{display:block;height:100%}.bb i:first-child{box-shadow:0 0 14px rgba(255,90,31,.5) inset}.bv{text-align:right;font-size:15px}
.leg{font-size:12.5px;color:var(--mute)}.leg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin:0 5px 0 12px;vertical-align:middle}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin:14px 0}
.kpi div{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px}.kpi b{display:block;font-size:30px;color:var(--fire);text-shadow:0 0 16px rgba(255,90,31,.5)}
.kpi span{font-size:13px;color:#c9ced6}
.no li{margin:6px 0}
@media(max-width:700px){.br{grid-template-columns:1fr 60px}.br .bl{grid-column:1/-1}}
</style>""")
    body = (css
            + "<h1>Quartiers prioritaires : combien d'argent public par habitant ?</h1>"
            "<p class=\"lead\">On entend souvent parler d'« investissement massif » dans les banlieues. Que disent les données ouvertes ? Cette page compare ce que l'État verse "
            "chaque année aux communes, par habitant, selon la part de leurs habitants qui vivent en quartier prioritaire de la politique de la ville (QPV). "
            "Ce n'est qu'une partie du tableau : lisez aussi ce qui n'est pas mesuré. "
            '<a href="' + BASE_PATH + '/argent-quartiers">Version courte, avec carte</a>.</p>'
            '<div class="kpi"><div><b>' + frn(fr_["pop_qpv"]) + "</b><span>habitants vivent en quartier prioritaire en métropole (population des QPV retenue pour la DSU, " + str(an) + ")</span></div>"
            "<div><b>" + ("%.2f" % (fr_["dsu_total"] / 1e9)).replace(".", ",") + " Md\u20ac</b><span>de dotation de solidarité urbaine versés aux communes en " + str(an) + "</span></div>"
            "<div><b>" + _eur(g10["25plus"]["dsu_hab"]) + "</b><span>de DSU par habitant dans les communes de 10 000 habitants et plus où 25 % ou plus des habitants vivent en QPV, contre "
            + _eur(g10["aucun"]["dsu_hab"]) + " dans celles sans QPV</span></div>"
            "<div><b>" + _eur(d["besoins"]["groupes"]["10000"]["aucun"]["total_par_pauvre"]) + " / " + _eur(d["besoins"]["groupes"]["10000"]["25plus"]["total_par_pauvre"])
            + "</b><span>de dotations totales par personne pauvre, sans QPV puis avec 25 % et plus d'habitants en QPV : " + fmt(rp) + " fois plus, contre " + fmt(rh) + " fois par habitant</span></div></div>"
            "<h2>Dotations de l'État par habitant (" + str(an) + ")</h2>"
            '<div class="mt"><button type="button" class="chip on" data-k="10000" style="--c:#ff5a1f"><i></i>Communes de 10\u202f000 habitants et plus</button>'
            '<button type="button" class="chip" data-k="toutes" style="--c:#ff5a1f"><i></i>Toutes les communes</button></div>'
            + bars("10000") + bars("toutes").replace('class="bars"', 'class="bars" style="display:none"', 1)
            + '<p class="meta">Dotations de fonctionnement versées par l\'État aux communes (DGF) : DSU, DSR et dotation forfaitaire, hors dotation nationale de péréquation. Chiffres divisés par la population INSEE de chaque groupe de communes. '
            "La comparaison à communes de 10\u202f000 habitants et plus évite l'effet de la taille : la DSU ne concerne que les communes de plus de 5\u202f000 habitants.</p>"
            "<h2>Rapporté aux besoins : par personne pauvre</h2>"
            "<p>Les communes avec beaucoup de QPV sont aussi nettement plus pauvres : " + str(nb10["25plus"]["taux_pauvrete"]).replace(".", ",") + " % de la population sous le seuil de pauvreté, contre "
            + str(nb10["aucun"]["taux_pauvrete"]).replace(".", ",") + " % dans les communes comparables sans QPV (Insee, " + str(d["besoins"]["filosofi"]) + "). Rapportées au nombre de personnes pauvres, "
            "l'écart se réduit nettement (" + fmt(rh) + " fois par habitant, " + fmt(rp) + " fois par personne pauvre) : <strong>" + _eur(nb10["aucun"]["total_par_pauvre"]) + "</strong> par personne pauvre sans QPV, <strong>"
            + _eur(nb10["25plus"]["total_par_pauvre"]) + "</strong> avec 25 % et plus d'habitants en QPV. La DSU pèse bien plus lourd dans les secondes (" + _eur(nb10["25plus"]["dsu_par_pauvre"])
            + " contre " + _eur(nb10["aucun"]["dsu_par_pauvre"]) + "), le reste étant la dotation forfaitaire et la DSR, qui ne dépendent pas directement de la pauvreté.</p>"
            + needs("10000") + needs("toutes").replace('class="bars"', 'class="bars" style="display:none"', 1)
            + '<p class="meta">Personnes pauvres : sous 60 % du niveau de vie médian, calculées au niveau de la commune, pas du quartier. Les trois sources n\'ont pas le même millésime : '
            "dotations " + str(d["besoins"]["dotations"]) + ", pauvreté " + str(d["besoins"]["filosofi"]) + ", RSA et prime d'activité décembre " + d["besoins"]["caf"][:4]
            + " (personnes couvertes, effectifs arrondis par la CAF, rapportées aux habitants). Les communes dont les données de revenus sont couvertes par le secret statistique manquent, surtout parmi les petites. "
            "Ce n'est pas l'argent reçu par ces personnes : c'est une façon de mettre l'effort en regard des besoins.</p>"
            + inv_html + eq_html + fo_html + bpe_html + qs_html + evo + "<h2>Les communes dont une grande part des habitants vit en QPV</h2>"
            '<table class="dtab"><thead><tr><th>Commune</th><th>Habitants</th><th>Part en QPV</th><th>DSU par hab.</th><th>Total par hab.</th></tr></thead><tbody>' + top + "</tbody></table>"
            '<p class="meta">Les 15 communes comptant le plus d\'habitants en QPV parmi celles où cette part atteint 25 %. '
            '<a href="' + BASE_PATH + '/qpv-dotations-' + str(an) + '.csv">Télécharger les ' + str(d.get("n_csv", "")) + " communes avec QPV (CSV)</a>.</p>"
            "<h2>Ce que cette page ne mesure pas</h2>"
            '<div class="card"><ul class="no">'
            "<li><strong>L'investissement.</strong> Les dotations ci-dessus sont des dotations de fonctionnement ; la section sur l'investissement ne couvre que les subventions de l'État aux projets des communes (DETR, DSIL, DPV). La rénovation urbaine (ANRU) est aussi de l'investissement : le nouveau programme (NPNRU) "
            "annonce 12 Md\u20ac de concours financiers pour 480 quartiers où vivent environ 3 millions d'habitants, soit de l'ordre de 4\u202f000 \u20ac par habitant sur toute la durée du programme "
            "(2015 à 2034, donc de l'ordre de 200 \u20ac par an). C'est un calcul à partir de chiffres globaux : les montants par quartier ne sont pas publiés en open data, "
            "seulement dans des annexes de conventions.</li>"
            "<li><strong>Le droit commun.</strong> L'essentiel de la dépense publique (écoles, police, transports, santé) n'est pas ventilé par quartier. La Cour des comptes constate que "
            "son identification a peu progressé depuis 2012. Aucune donnée ouverte ne permet de dire si un habitant de QPV reçoit plus ou moins de dépense publique totale qu'un autre.</li>"
            "<li><strong>Les prestations sociales.</strong> Le RSA et la prime d'activité ne figurent qu'en repère des besoins. RSA, APL, allocations sont des droits individuels, non rattachés à un quartier dans ces fichiers ; "
            "les montants d'APL par commune ne sont pas inclus.</li>"
            "<li><strong>L'effet de la taille et de l'histoire.</strong> La dotation forfaitaire dépend en partie de la taille de la commune et de son passé, pas seulement de sa pauvreté.</li>"
            "<li><strong>Les millésimes.</strong> Dotations " + str(an) + " (fichiers de la DGCL repris par l'OFGL), pauvreté " + str(d["besoins"]["filosofi"]) + ", RSA et prime d'activité décembre "
            + d["besoins"]["caf"][:4] + " : les années diffèrent. Les montants sont en euros courants, sans correction de l'inflation.</li></ul></div>"
            "<h2>Comment lire ces chiffres</h2>"
            "<p>Les communes où une grande part des habitants vit en QPV reçoivent nettement plus de dotations de l'État par habitant : c'est le but de la DSU, qui cible les communes "
            "à fortes difficultés sociales. Mais ces communes sont aussi bien plus pauvres, et rapporté au nombre de personnes pauvres l'écart se réduit nettement, de " + fmt(rh) + " fois à " + fmt(rp) + " fois, sans disparaître. "
            + ((("Côté subventions d'investissement de l'État aux communes (DETR, DSIL, DPV), l'écart est de " + fmt(ri) + " fois par habitant et de " + fmt(rip) + " fois par personne pauvre. ") if ri else "")) + eq_sentence + fo_sentence + bpe_sentence + qs_sentence + 
            "Ces données ne permettent ni de confirmer ni de réfuter l'idée d'un « investissement massif » : il faudrait pour cela les montants "
            "de la rénovation urbaine par quartier et la part du droit commun, qui ne sont pas publiés de manière exploitable. Nous les publierons quand ils le seront.</p>"
            "<h2>Sources</h2><ul>"
            '<li><a href="https://data.ofgl.fr/explore/dataset/dotations-communes/" rel="noopener" target="_blank">OFGL (données DGCL), critères de répartition des dotations versées par l\'État aux collectivités</a> (fichier communes ' + str(an) + ")</li>"
            '<li><a href="https://www.insee.fr/fr/statistiques/8210600" rel="noopener" target="_blank">Insee, population des quartiers de la politique de la ville 2024</a></li>'
            '<li><a href="https://www.insee.fr/fr/statistiques/7756729" rel="noopener" target="_blank">Insee, Filosofi : revenus, pauvreté et niveau de vie ' + str(d["besoins"]["filosofi"]) + " par commune</a></li>"
            '<li><a href="https://www.data.gouv.fr/datasets/rsa-et-pa-repartition-des-allocataires-communal" rel="noopener" target="_blank">CAF, RSA et prime d\'activité par commune</a></li>'
            '<li><a href="https://www.data.gouv.fr/datasets/projets-finances-par-les-dotations-de-soutien-a-linvestissement-des-collectivites-territoriales" rel="noopener" target="_blank">DGCL, projets financés par les dotations d\'investissement (DETR, DSIL, DPV)</a> 2018-2025</li>'
            '<li><a href="https://data.ofgl.fr/explore/dataset/ofgl-base-communes/" rel="noopener" target="_blank">OFGL, comptes des communes (fonctionnement et équipement)</a> 2018-2024</li>'
            '<li><a href="https://www.insee.fr/fr/metadonnees/source/operation/s2216/presentation" rel="noopener" target="_blank">Insee, base permanente des équipements 2025</a></li>'
            '<li><a href="https://anruopendata.opendatasoft.com/explore/assets/contrats-npnru/" rel="noopener" target="_blank">ANRU OpenData, contrats NPNRU</a> (liste des contrats, sans montants)</li>'
            '<li><a href="https://www.anru.fr/sites/default/files/media/downloads/questions-reponses-amplification-du-npnru.pdf" rel="noopener" target="_blank">ANRU, questions-réponses sur le NPNRU</a> (enveloppe de 12 Md\u20ac)</li>'
            '<li><a href="https://www.banquedesterritoires.fr/la-reforme-de-la-politique-de-la-ville-ne-convainc-qua-moitie-la-cour-des-comptes" rel="noopener" target="_blank">Banque des Territoires, la Cour des comptes sur la politique de la ville</a></li></ul>'
            '<p class="meta"><a href="' + BASE_PATH + '/departements">Voir les départements</a> · <a href="' + BASE_PATH + '/sources">Sources et méthode</a></p>'
            "<script>(function(){var bs=document.querySelectorAll('.bars'),ch=document.querySelectorAll('.mt .chip');ch.forEach(function(b){b.onclick=function(){"
            "ch.forEach(function(x){x.classList.toggle('on',x===b)});bs.forEach(function(e){e.style.display=e.getAttribute('data-k')===b.getAttribute('data-k')?'':'none'})}})})();</script>")
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title.split(" | ")[0], "url": url, "inLanguage": "fr-FR", "description": desc,
          "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"}}
    return 200, shell(title, desc, url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


QPVC = {"mtime": 0, "d": None}
SIMPLE_CSS = """
.sb{margin:16px 0 22px}.sb .row{margin:0 0 20px}.sb .row b.t{display:block;font-size:16px;margin-bottom:6px}
.sb .ln{display:grid;grid-template-columns:150px 1fr 56px;gap:10px;align-items:center;margin:4px 0;font-size:13px;color:#c9ced6}
.sb .tr{height:20px;background:#10131a;border:1px solid var(--line);border-radius:6px;overflow:hidden}.sb .tr i{display:block;height:100%}
.sb .ln span:last-child{text-align:right;color:#fff;font-weight:700}.sb .verdict{margin:4px 0 0;font-size:14px;color:var(--fire);font-weight:700}
.stack{display:flex;height:34px;border:1px solid var(--line);border-radius:8px;overflow:hidden;margin:12px 0 6px}.stack i{display:block;height:100%}
.slg{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:#c9ced6;margin:0 0 12px}.slg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
#qmap{height:480px;border-radius:12px;border:1px solid var(--line);background:#0e1117}
.qleg{display:flex;flex-wrap:wrap;gap:6px 14px;font-size:12.5px;color:#c9ced6;margin:8px 0}.qleg i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:middle}
.more{display:block;background:var(--panel);border:1px solid var(--fire);border-radius:14px;padding:16px 18px;margin:18px 0;color:#fff;text-decoration:none}
.more b{display:block;font-size:19px;color:var(--fire)}.more span{font-size:14px;color:#c9ced6}
@media(max-width:700px){.sb .ln{grid-template-columns:1fr 50px}.sb .ln span:first-child{grid-column:1/-1}#qmap{height:380px}}
"""


def qpv_carte():
    path = os.path.join(HERE, "qpv_carte.json")
    try:
        m = os.path.getmtime(path)
        if m != QPVC["mtime"]:
            QPVC["d"], QPVC["mtime"] = json.load(open(path, encoding="utf-8")), m
    except Exception as ex:
        print("qpv_carte :", ex)
    return QPVC["d"]


QPVS = {"mtime": 0, "d": None}
SERV_EXCL = ("gymnase", "piscine")  # coordonnées imprécises dans la BPE (qualité 3) : non comparables
CMP_CSS = """
.cmp{margin:14px 0}.cmp .l{display:grid;grid-template-columns:200px 1fr 64px;gap:10px;align-items:center;margin:5px 0;font-size:13.5px;color:#c9ced6}
.cmp .tr{position:relative;height:18px;background:#10131a;border:1px solid var(--line);border-radius:6px;overflow:hidden}.cmp .tr i{display:block;height:100%}
.cmp .tr u{position:absolute;top:0;bottom:0;width:2px;background:#fff;opacity:.55}.cmp .l span:last-child{text-align:right;color:#fff;font-weight:700}
@media(max-width:700px){.cmp .l{grid-template-columns:1fr 54px}.cmp .l span:first-child{grid-column:1/-1}}
"""


def qpv_services():
    path = os.path.join(HERE, "qpv_services.json")
    try:
        m = os.path.getmtime(path)
        if m != QPVS["mtime"]:
            QPVS["d"], QPVS["mtime"] = json.load(open(path, encoding="utf-8")), m
    except Exception as ex:
        print("qpv_services :", ex)
    return QPVS["d"]


def serv_compare():
    """Équipements dans les quartiers prioritaires, pour 10 000 habitants, comparés au reste des mêmes communes (communes de 10 000 habitants et plus)."""
    sd = qpv_services()
    if not sd:
        return None
    g = sd["groupes"]
    rows = []
    for t in sd["themes"]:
        k = t["cle"]
        if k in SERV_EXCL:
            continue
        q, m, s = g["quartiers"][k], g["memes"][k], g["sans"][k]
        ok = m >= 1.0 and g["quartiers"]["n"][k] >= 30
        rows.append({"k": k, "lib": t["lib"], "dom": t["dom"], "q": q, "m": m, "s": s, "nq": g["quartiers"]["n"][k], "r": (q / m) if ok else None})
    return {"rows": rows, "nq": len(sd["quartiers"]), "communes": g["quartiers"]["communes"], "pop": g["quartiers"]["pop"], "annee": sd["annee"]}


def serv_words(r):
    if r >= 1.1:
        return ("%.1f" % r).replace(".", ",") + " fois plus" if r >= 1.5 else "%+.0f %%" % ((r - 1) * 100)
    if r <= 1 / 1.1:
        return ("%.1f" % (1 / r)).replace(".", ",") + " fois moins" if r <= 0.67 else "−%.0f %%" % ((1 - r) * 100)
    return "à peu près autant"


def serv_table_html():
    sc = serv_compare()
    if not sc:
        return "", ""
    pct = lambda r: ("%+.0f" % ((r - 1) * 100)).replace("-", "−") + " %"
    ok = [r for r in sc["rows"] if r["r"] is not None]
    low = sorted([r for r in ok if r["r"] <= 1 / 1.1], key=lambda r: r["r"])[:4]
    high = sorted([r for r in ok if r["r"] >= 1.1], key=lambda r: -r["r"])[:3]
    sentence = "Dans le périmètre des quartiers eux-mêmes, par rapport au reste de leur commune, les équipements sont "
    if low:
        sentence += "moins nombreux pour : " + ", ".join("%s (%s)" % (r["lib"].lower(), pct(r["r"])) for r in low)
    if low and high:
        sentence += " ; "
    if high:
        sentence += "plus nombreux pour : " + ", ".join("%s (%s)" % (r["lib"].lower(), pct(r["r"])) for r in high)
    sentence += ". "
    rows, dom = [], None
    for r in sc["rows"]:
        if r["dom"] != dom:
            dom = r["dom"]
            rows.append('<tr><td colspan="5"><b>%s</b></td></tr>' % esc(dom))
        f = lambda x: ("%.1f" % x).replace(".", ",")
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (esc(r["lib"]), f(r["q"]), f(r["m"]), f(r["s"]), pct(r["r"]) if r["r"] is not None else "n.s."))
    html_ = ("<h2>Dans le quartier lui-même</h2>"
             "<p>Les chiffres ci-dessus portent sur la commune entière. L'Insee localise pourtant chaque équipement et indique s'il se trouve dans un quartier prioritaire. "
             "Pour " + frn(sc["nq"]) + " quartiers (" + frn(sc["pop"]) + " habitants, communes de 10 000 habitants et plus), on compte les équipements situés dans le périmètre du quartier, pour 10 000 habitants du quartier :</p>"
             '<table class="dtab"><thead><tr><th>Équipement</th><th>Dans les quartiers</th><th>Reste des mêmes communes</th><th>Communes sans QPV</th><th>Écart (quartiers contre reste de la commune)</th></tr></thead><tbody>'
             + "".join(rows) + "</tbody></table>"
             '<p class="meta">Source : <a href="https://www.insee.fr/fr/statistiques/8217525" rel="noopener" target="_blank">Insee, base permanente des équipements ' + str(sc["annee"]) + ", fichier détail géolocalisé</a> ; périmètres QPV 2024. "
             "Seuls comptent les équipements dans le périmètre : un service à 200 mètres, de l'autre côté de la rue, est compté dans le reste de la commune. Un nombre ne dit rien de la taille, des horaires ou de la qualité. "
             "Les équipements dont la position est jugée mauvaise par l'Insee sont écartés (environ 5 %). Gymnases et bassins de natation ne sont pas comparés : leurs coordonnées sont imprécises. "
             "« n.s. » : trop peu d'équipements pour comparer.</p>")
    return html_, sentence


def serv_bars_html():
    sc = serv_compare()
    if not sc:
        return ""
    ok = sorted([r for r in sc["rows"] if r["r"] is not None], key=lambda r: r["r"])
    mx = max(150.0, max(100.0 * r["r"] for r in ok))
    lines = []
    for r in ok:
        v = 100.0 * r["r"]
        col = "#6b7480" if 1 / 1.1 < r["r"] < 1.1 else ("#ff5a1f" if r["r"] < 1 else "#17c3a2")
        lines.append('<div class="l"><span>%s</span><div class="tr"><i style="width:%.1f%%;background:%s"></i><u style="left:%.1f%%"></u></div><span title="%s">%d</span></div>'
                     % (esc(r["lib"]), 100.0 * v / mx, col, 100.0 * 100 / mx, esc(serv_words(r["r"])), round(v)))
    less = [r for r in ok if r["r"] <= 1 / 1.1]
    more = [r for r in ok if r["r"] >= 1.1]
    lec = ""
    if less:
        lec += "<strong>Moins nombreux dans le quartier :</strong> " + esc(", ".join(r["lib"].lower() + " (" + serv_words(r["r"]) + ")" for r in sorted(less, key=lambda r: r["r"])[:5])) + ". "
    if more:
        lec += "<strong>Plus nombreux :</strong> " + esc(", ".join(r["lib"].lower() + " (" + serv_words(r["r"]) + ")" for r in sorted(more, key=lambda r: -r["r"])[:4])) + "."
    return ("<h2>Et dans le quartier lui-même ?</h2>"
            "<p>Les chiffres ci-dessus portent sur la commune. Mais l'Insee localise chaque équipement : on peut compter ceux qui sont dans le périmètre même des " + frn(sc["nq"])
            + " quartiers prioritaires. Base 100 : le reste de la même commune, pour le même nombre d'habitants.</p>"
            '<div class="cmp">' + "".join(lines) + "</div>"
            '<p class="meta">' + lec + " Orange : moins qu'ailleurs dans la commune ; vert : plus ; gris : à peu près pareil (écart de moins de 10 %). La barre blanche marque 100.</p>"
            '<p class="meta">Seuls comptent les équipements situés dans le périmètre du quartier (un service juste de l\'autre côté de la rue n\'est pas compté). Un nombre ne dit rien de la taille, des horaires ou de la qualité. '
            'Source : <a href="https://www.insee.fr/fr/statistiques/8217525" rel="noopener" target="_blank">Insee, base permanente des équipements ' + str(sc["annee"]) + "</a>, communes de 10 000 habitants et plus.</p>")


def qpv_simple_page():
    d = qpv_data()
    url = BASE_URL + "/argent-quartiers"
    if not d:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow")
    fmt = lambda x: ("%.1f" % x).replace(".", ",")
    g10, nb = d["groupes"]["10000"], d["besoins"]["groupes"]["10000"]
    # 1. barres base 100 : commune sans QPV = 100
    rows = [("Dotations de l'État par habitant", g10["25plus"]["total_hab"] / g10["aucun"]["total_hab"], "short"),
            ("Dotations de l'État par personne pauvre", nb["25plus"]["total_par_pauvre"] / nb["aucun"]["total_par_pauvre"], "short")]
    ivd, eqd, fod = d.get("invest"), d.get("equip"), d.get("fonct")
    if ivd:
        i10 = ivd["groupes"]["10000"]
        rows.append(("Subventions d'investissement de l'État par personne pauvre", i10["25plus"]["total_par_pauvre_an"] / i10["aucun"]["total_par_pauvre_an"], "short"))
    if eqd:
        ec = eqd["communes"]["10000"]
        rows.append(("Dépenses d'équipement de la commune par personne pauvre", ec["25plus"]["eq_par_pauvre_an"] / ec["aucun"]["eq_par_pauvre_an"], "short"))
    if fod:
        fg = fod["groupes"]
        rows.append(("Dépenses de fonctionnement de la commune par personne pauvre", fg["25plus"]["pauvre"]["Dépenses de fonctionnement"] / fg["aucun"]["pauvre"]["Dépenses de fonctionnement"], "short"))
    mx = max(max(r for _, r, _ in rows), 1.0)

    def verdict(r):
        if r >= 1.1:
            return fmt(r) + " fois plus"
        if r <= 1 / 1.1:
            return fmt(1 / r) + " fois moins"
        return "à peu près autant"

    bars = "".join(
        '<div class="row"><b class="t">%s</b><div class="ln"><span>Sans quartier prioritaire</span><div class="tr"><i style="width:%.1f%%;background:#6b7480"></i></div><span>100</span></div>'
        '<div class="ln"><span>25 %% et plus en QPV</span><div class="tr"><i style="width:%.1f%%;background:#ff5a1f;box-shadow:0 0 14px rgba(255,90,31,.5) inset"></i></div><span>%d</span></div>'
        '<p class="verdict">%s</p></div>' % (esc(lab), 100.0 / mx, 100.0 * r / mx, round(100 * r), esc(verdict(r))) for lab, r, _ in rows)
    plus = [lab for lab, r, _ in rows if r >= 1.1]
    meme = [lab for lab, r, _ in rows if 1 / 1.1 < r < 1.1]
    moins = [lab for lab, r, _ in rows if r <= 1 / 1.1]
    lec = []
    short = lambda labs: " ; ".join(l[0].lower() + l[1:] for l in labs)
    if plus:
        lec.append("<strong>Beaucoup plus :</strong> " + esc(short(plus)))
    if meme:
        lec.append("<strong>À peu près autant :</strong> " + esc(short(meme)))
    if moins:
        lec.append("<strong>Moins :</strong> " + esc(short(moins)))
    reading = '<p class="meta">' + " · ".join(lec) + ".</p>" if lec else ""
    # 2. carte
    carte = qpv_carte() or []
    comm = {c["code"]: c for c in FULL}
    pts = []
    for r in carte:
        c = comm.get(r["code"])
        if not c:
            continue
        pts.append({"n": r["nom"], "d": r["dep"], "la": round(c["lat"], 4), "lo": round(c["lon"], 4), "p": r["pop"], "q": r["pop_qpv"], "s": r["part"], "h": r["hab"], "v": r["pauvre"],
                    "u": "%s/ville/%s-%s" % (BASE_PATH, norm(r["nom"]).replace(" ", "-"), r["code"])})
    ref = {"h": round(g10["aucun"]["total_hab"]), "v": round(nb["aucun"]["total_par_pauvre"])}
    cdata = json.dumps({"pts": pts, "ref": ref}, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    # 3. l'écart de dépenses de fonctionnement
    fo_html = ""
    if fod:
        ga, gq = fod["groupes"]["aucun"]["hab"], fod["groupes"]["25plus"]["hab"]
        tot = "Dépenses de fonctionnement"
        parts = [("Frais de personnel", "Frais de personnel", "#ff5a1f"), ("Dépenses d'intervention", "Subventions et transferts versés", "#9b6bff"),
                 ("Charges financières", "Intérêts de la dette", "#17c3a2"), ("Achats et charges externes", "Achats et charges", "#38bdf8"), ("Autres dépenses de fonctionnement", "Autres", "#6b7480")]
        gap = gq[tot] - ga[tot]
        vals = [(lab, gq[k] - ga[k], col) for k, lab, col in parts]
        conc = gq["Concours de l'Etat"] - ga["Concours de l'Etat"]
        ann = gq["Annuité de la dette"] - ga["Annuité de la dette"] if "Annuité de la dette" in gq else None
        stack = "".join('<i style="width:%.2f%%;background:%s" title="%s : +%s par habitant"></i>' % (100.0 * v / sum(x for _, x, _ in vals if x > 0), c, esc(l), _eur(v)) for l, v, c in vals if v > 0)
        leg = "".join('<span><i style="background:%s"></i>%s <b>%s</b></span>' % (c, esc(l), ("+" if v >= 0 else "−") + _eur(abs(v))) for l, v, c in vals)
        dette = ""
        if ann is not None and conc > 0:
            dette = (" Le remboursement de la dette (intérêts et capital) pèse <strong>" + ("+" if ann >= 0 else "−") + _eur(abs(ann)) + "</strong> par habitant, soit environ "
                     + str(int(round(100.0 * ann / conc))) + " % des " + _eur(conc) + " de dotations en plus : une part, pas l'essentiel.")
        fo_html = (
            "<h2>Où va l'argent en plus ?</h2>"
            "<p>Les communes à forte part de QPV dépensent <strong>" + _eur(gap) + "</strong> de plus par habitant et par an pour leur fonctionnement (" + _eur(gq[tot]) + " contre " + _eur(ga[tot]) + "). "
            "Elles reçoivent <strong>" + _eur(conc) + "</strong> de plus de l'État par habitant. Voici où va l'écart de dépenses, poste par poste :</p>"
            '<div class="stack">' + stack + '</div><div class="slg">' + leg + "</div>"
            "<p>" + dette.strip() + " Leur épargne brute est du même ordre que celle des autres (" + _eur(gq["Epargne brute"]) + " contre " + _eur(ga["Epargne brute"]) + ") : les recettes en plus sont dépensées.</p>"
            '<p class="meta">La base ne détaille pas par fonction (école, social, sécurité) : on voit la nature des dépenses, pas leur usage. Euros courants, moyenne 2018-2024, communes de 10 000 habitants et plus.</p>')
    js = r"""(function(){
var D=JSON.parse(document.getElementById('qd').textContent),cur='h';
var map=L.map('qmap',{zoomControl:true,scrollWheelZoom:false,tap:false,minZoom:5,maxZoom:11,zoomSnap:.25}).fitBounds([[41.2,-5.4],[51.2,9.8]]);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:11,attribution:'© OpenStreetMap',className:'dark-tiles'}).addTo(map);
map.zoomControl.setPosition('bottomright');map.on('focus',function(){map.scrollWheelZoom.enable()});map.on('blur',function(){map.scrollWheelZoom.disable()});
var bins=[[0,.8,'#38bdf8','moins que les communes sans QPV'],[.8,1.25,'#9aa3ad','autant'],[1.25,2,'#ffb020','1,25 à 2 fois plus'],[2,3,'#ff7a1f','2 à 3 fois plus'],[3,1e9,'#ff3b1f','plus de 3 fois plus']];
function col(r){for(var i=0;i<bins.length;i++){if(r>=bins[i][0]&&r<bins[i][1])return bins[i][2]}return '#9aa3ad'}
function f1(x){return (Math.round(x*10)/10).toString().replace('.',',')}
function nf(x){return String(Math.round(x)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')}
var order=D.pts.slice().sort(function(a,b){return b.q-a.q}),marks=[];
order.forEach(function(p){var m=L.circleMarker([p.la,p.lo],{radius:Math.max(3,Math.min(24,Math.sqrt(p.q)/16)),weight:1,color:'#0b0d12',fillOpacity:.8});
 m.bindTooltip('',{sticky:true});m.on('click',function(){location.href=p.u});m.addTo(map);marks.push([m,p])});
function paint(){var ref=D.ref[cur];marks.forEach(function(x){var m=x[0],p=x[1],v=p[cur];
 if(v==null){m.setStyle({fillOpacity:0,opacity:0});m.setTooltipContent('<b>'+p.n+'</b><br>données de pauvreté absentes');return}
 var r=v/ref;m.setStyle({fillColor:col(r),fillOpacity:.8,opacity:1});
 m.setTooltipContent('<b>'+p.n+'</b> ('+p.d+')<br>'+f1(p.s)+' % des habitants en quartier prioritaire ('+nf(p.q)+')<br>'+nf(v)+' € '+(cur=='h'?'par habitant':'par personne pauvre')+'<br>soit '+f1(r)+' fois les communes sans QPV ('+nf(ref)+' €)')});
 document.getElementById('qleg').innerHTML=bins.map(function(b){return '<span><i style="background:'+b[2]+'"></i>'+b[3]+'</span>'}).join('')}
document.querySelectorAll('.mt .chip').forEach(function(b){b.onclick=function(){cur=b.getAttribute('data-k');
 document.querySelectorAll('.mt .chip').forEach(function(x){x.classList.toggle('on',x===b)});paint()}});
paint();
})();"""
    title = "Argent public et quartiers prioritaires : l'essentiel en une page | ça brûle"
    desc = ("Les communes où au moins un quart des habitants vivent en quartier prioritaire reçoivent beaucoup plus de dotations de l'État par habitant, mais l'écart fond une fois rapporté à la pauvreté. "
            "Carte, comparaisons et limites, en clair, avec les sources ouvertes.")
    body = ("<style>" + DEP_CSS + SIMPLE_CSS + CMP_CSS + "</style>"
            '<link rel="stylesheet" href="' + BASE_PATH + '/lib/leaflet.css"><script src="' + BASE_PATH + '/lib/leaflet.js"></script>'
            "<h1>Banlieues : où va l'argent public ?</h1>"
            "<p class=\"lead\">On parle d'« investissement massif » dans les quartiers populaires. Voici ce que montrent les chiffres ouverts, en clair. "
            "On compare les communes de 10 000 habitants ou plus où au moins un quart des habitants vivent en quartier prioritaire (QPV) à celles qui n'en ont aucun. "
            '<a href="' + BASE_PATH + '/habitant-type">Voir en animation : 100 habitants d\'un quartier prioritaire</a>.</p>'
            "<h2>Combien reçoivent-elles ?</h2>"
            '<p class="meta">Base 100 : une commune sans quartier prioritaire. Chaque barre orange dit combien reçoit ou dépense une commune à forte part de QPV pour 100 reçus ou dépensés ailleurs.</p>'
            '<div class="sb">' + bars + "</div>" + reading
            + "<h2>Où sont ces communes ?</h2>"
            '<p>Chaque rond est une commune qui compte au moins un quartier prioritaire. Plus il est gros, plus il y a d\'habitants en quartier. La couleur dit si la commune reçoit plus ou moins que la moyenne des communes sans QPV. '
            "Cliquez sur un rond pour la fiche de la commune.</p>"
            '<div class="mt"><button type="button" class="chip on" data-k="h" style="--c:#ff5a1f"><i></i>Dotations par habitant</button>'
            '<button type="button" class="chip" data-k="v" style="--c:#ff5a1f"><i></i>Dotations par personne pauvre</button></div>'
            '<div id="qmap" role="img" aria-label="Carte des communes avec quartier prioritaire"></div><div class="qleg" id="qleg"></div>'
            '<p class="meta">Dotations de l\'État aux communes en ' + str(d["annee"]) + " (DSU, DSR et dotation forfaitaire). Personnes pauvres : sous 60 % du niveau de vie médian (Insee, " + str(d["besoins"]["filosofi"])
            + "), au niveau de la commune. La référence est la moyenne des communes de 10 000 habitants et plus sans QPV.</p>"
            + serv_bars_html() + fo_html
            + "<h2>Ce que ces chiffres ne montrent pas</h2>"
            '<div class="card"><ul class="no">'
            "<li><strong>La rénovation urbaine (ANRU).</strong> L'ANRU annonce 12 Md€ pour rénover 480 quartiers : ce n'est pas dans ces chiffres, qui ne couvrent que l'argent des communes et les dotations et subventions de l'État.</li>"
            "<li><strong>Les écoles, la police, les transports, le RSA, les APL.</strong> Cette dépense publique n'est pas ventilée par quartier en open data.</li>"
            "<li><strong>L'argent dans le quartier.</strong> Les dotations et les dépenses sont celles de la commune entière : aucun montant n'est publié par quartier. Seuls les services sont comptés dans le périmètre du quartier.</li></ul></div>"
            "<p>Conclusion prudente : par habitant, ces communes reçoivent nettement plus ; par personne pauvre, l'avantage fond, et leurs dépenses d'équipement sont plus faibles. "
            "Il faudrait les montants ANRU par quartier et le droit commun pour dire si l'effort est « massif » ou non.</p>"
            '<a class="more" href="' + BASE_PATH + '/quartiers-prioritaires"><b>Pour aller plus loin : tous les chiffres, les méthodes et les sources</b>'
            "<span>La page détaillée : dotations par groupe de communes, subventions DETR, DSIL et DPV, dépenses d'équipement et de fonctionnement, accès aux services, évolution depuis 2019, limites.</span></a>"
            '<p class="meta"><a href="' + BASE_PATH + '/departements">Voir les départements</a> · <a href="' + BASE_PATH + '/sources">Sources et méthode</a></p>'
            '<script id="qd" type="application/json">' + cdata + "</script><script>" + js + "</script>")
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title.split(" | ")[0], "url": url, "inLanguage": "fr-FR", "description": desc,
          "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"}}
    return 200, shell(title, desc, url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


HAB_CSS = """
.sc{margin:30px 0;padding:20px 18px;background:var(--panel);border:1px solid var(--line);border-radius:16px}
.sc h2{margin:0 0 6px;font-size:21px}.sc .q{color:#c9ced6;margin:0 0 14px;font-size:15px}
.sc .cap{margin:14px 0 0;font-size:14px;color:#c9ced6}.sc .cap b{color:#fff}
.sc .src{margin:8px 0 0;font-size:12px;color:var(--mute)}
.grids{display:flex;gap:22px;flex-wrap:wrap;justify-content:center}.gr{flex:1 1 200px;max-width:260px}
.gr .gl{text-align:center;font-size:13px;color:#c9ced6;margin:0 0 8px;min-height:34px}
.gr .big{display:block;text-align:center;font-size:46px;font-weight:800;line-height:1.1;margin-top:8px;color:#9aa3ad}
.gr.hot .big{color:var(--fire);text-shadow:0 0 18px rgba(255,90,31,.5)}.gr .big small{font-size:15px;font-weight:600;color:#c9ced6}
.dots{display:grid;grid-template-columns:repeat(10,1fr);gap:5px}.dots i{aspect-ratio:1;border-radius:50%;background:#262c37;transition:background .25s,transform .25s}
.dots i.on{background:var(--fire);transform:scale(1.14);box-shadow:0 0 8px rgba(255,90,31,.6)}.dots i.on2{background:#38bdf8;transform:scale(1.14);box-shadow:0 0 8px rgba(56,189,248,.6)}
.dots.ref i.on,.dots.ref i.on2{background:#8a94a3;box-shadow:none}
.hb{margin:14px 0}.hb .t{font-size:14px;margin:0 0 6px;color:#c9ced6}.hb .r{display:grid;grid-template-columns:190px 1fr 96px;gap:10px;align-items:center;margin:5px 0;font-size:13px;color:#c9ced6}
.hb .tr{height:24px;background:#10131a;border:1px solid var(--line);border-radius:7px;overflow:hidden}.hb .fill{display:block;height:100%;width:0;transition:width 1.3s cubic-bezier(.2,.7,.2,1)}
.go .hb .fill{width:var(--w)}.hb .v{text-align:right;color:#fff;font-weight:800;font-size:15px}
.stk .tr{display:flex}.stk .tr i{display:block;height:100%;width:0;transition:width 1.3s cubic-bezier(.2,.7,.2,1)}.go .stk .tr i{width:var(--w)}
.slg{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:13px;color:#c9ced6;margin:8px 0 0}.slg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px}
.ic{margin:12px 0}.ic .t{font-size:14px;color:#fff;margin:0 0 4px}.ic .ln{display:grid;grid-template-columns:150px 1fr 54px;gap:8px;align-items:center;font-size:12.5px;color:#c9ced6;margin:2px 0}
.ic .em{font-size:20px;line-height:1.25;min-height:26px}.ic .em span{display:inline-block;opacity:0;transform:scale(.3)}
.go .ic .em span{animation:pop .45s cubic-bezier(.3,1.6,.5,1) both;animation-delay:calc(var(--i)*70ms)}.ic .ln b{color:#fff;text-align:right}
.ic .ln.me b{color:var(--fire)}
@keyframes pop{to{opacity:1;transform:scale(1)}}
@media(max-width:700px){.hb .r{grid-template-columns:1fr 84px}.hb .r span:first-child{grid-column:1/-1}.ic .ln{grid-template-columns:1fr 46px}.ic .ln span:first-child{grid-column:1/-1}}
@media(prefers-reduced-motion:reduce){.hb .fill,.stk .tr i{transition:none}.go .ic .em span{animation:none;opacity:1;transform:none}}
"""

HAB_JS = r"""(function(){
var rm=window.matchMedia&&matchMedia('(prefers-reduced-motion: reduce)').matches;
function nf(x){return String(Math.round(x)).replace(/\B(?=(\d{3})+(?!\d))/g,' ')}
document.querySelectorAll('.dots').forEach(function(d){for(var k=0;k<100;k++){d.appendChild(document.createElement('i'))}});
function play(sc){
 if(sc.classList.contains('go'))return;sc.classList.add('go');
 sc.querySelectorAll('.dots').forEach(function(d){var n=parseInt(d.getAttribute('data-n')),c=d.getAttribute('data-c')||'on',it=d.children;
  for(var k=0;k<n;k++){(function(k){if(rm){it[k].className=c}else{setTimeout(function(){it[k].className=c},120+k*(1100/Math.max(n,1)))}})(k)}});
 sc.querySelectorAll('.cnt').forEach(function(e){var to=parseFloat(e.getAttribute('data-to')),t0=null;
  if(rm){e.textContent=nf(to);return}
  function st(t){if(!t0)t0=t;var p=Math.min(1,(t-t0)/1300),k=1-Math.pow(1-p,3);e.textContent=nf(to*k);if(p<1)requestAnimationFrame(st)}requestAnimationFrame(st)})}
var secs=document.querySelectorAll('.sc');
if('IntersectionObserver' in window){var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting){play(e.target);io.unobserve(e.target)}})},{threshold:.35});secs.forEach(function(s){io.observe(s)})}
else{secs.forEach(play)}
})();"""


def habitant_page():
    d = qpv_data()
    url = BASE_URL + "/habitant-type"
    if not d:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow")
    fmt = lambda x: ("%.1f" % x).replace(".", ",")
    nb = d["besoins"]["groupes"]["10000"]
    g10 = d["groupes"]["10000"]
    a, q = nb["aucun"], nb["25plus"]
    p0, p1 = int(round(a["taux_pauvrete"])), int(round(q["taux_pauvrete"]))
    r0, r1 = int(round(a["rsa_pa_100"])), int(round(q["rsa_pa_100"]))
    hab0, hab1 = g10["aucun"]["total_hab"], g10["25plus"]["total_hab"]
    pa0, pa1 = a["total_par_pauvre"], q["total_par_pauvre"]

    def grid(label, n, cls, hot, unit):
        return ('<div class="gr%s"><p class="gl">%s</p><div class="dots%s" data-n="%d" data-c="%s"></div><span class="big">%d<small> %s</small></span></div>'
                % (" hot" if hot else "", esc(label), "" if hot else " ref", n, cls, n, unit))

    s1 = ('<section class="sc"><h2>Sur 100 habitants, combien vivent sous le seuil de pauvreté ?</h2>'
          '<p class="q">Imaginons 100 habitants d\'une commune de 10 000 habitants ou plus, et comparons deux types de communes.</p>'
          '<div class="grids">' + grid("Communes sans quartier prioritaire", p0, "on", False, "sur 100") + grid("Communes où 25 % ou plus des habitants vivent en quartier prioritaire", p1, "on", True, "sur 100") + "</div>"
          '<p class="cap"><b>' + str(p1) + " habitants sur 100 contre " + str(p0) + "</b> : le seuil de pauvreté est de 60 % du niveau de vie médian. Ce sont des chiffres de la commune entière, pas du quartier.</p>"
          '<p class="src">Insee, Filosofi ' + str(d["besoins"]["filosofi"]) + ".</p></section>")
    s2 = ('<section class="sc"><h2>Combien sont couverts par le RSA ou la prime d\'activité ?</h2>'
          '<p class="q">Même comparaison, pour 100 habitants.</p>'
          '<div class="grids">' + grid("Communes sans quartier prioritaire", r0, "on2", False, "sur 100") + grid("Communes où 25 % ou plus des habitants vivent en quartier prioritaire", r1, "on2", True, "sur 100") + "</div>"
          '<p class="cap"><b>' + str(r1) + " sur 100 contre " + str(r0) + "</b> : il s'agit des personnes couvertes (allocataires, conjoints et enfants). Un habitant peut être pauvre sans toucher ces aides, et l'inverse.</p>"
          '<p class="src">CAF, RSA et prime d\'activité par commune, décembre ' + d["besoins"]["caf"][:4] + ".</p></section>")
    s3 =('<section class="sc"><h2>Combien l\'État verse-t-il à la commune, chaque année ?</h2>'
          '<p class="q">Dotations de fonctionnement (DSU, DSR, dotation forfaitaire), en euros par an.</p>'
          '<div class="hb"><p class="t">Par habitant</p>'
          '<div class="r"><span>Commune sans quartier prioritaire</span><div class="tr"><i class="fill" style="--w:%.1f%%;background:#6b7480"></i></div><span class="v"><span class="cnt" data-to="%d">0</span> €</span></div>'
          '<div class="r"><span>Commune à 25 %% ou plus en QPV</span><div class="tr"><i class="fill" style="--w:%.1f%%;background:#ff5a1f"></i></div><span class="v"><span class="cnt" data-to="%d">0</span> €</span></div></div>'
          '<div class="hb"><p class="t">Par personne pauvre</p>'
          '<div class="r"><span>Commune sans quartier prioritaire</span><div class="tr"><i class="fill" style="--w:%.1f%%;background:#6b7480"></i></div><span class="v"><span class="cnt" data-to="%d">0</span> €</span></div>'
          '<div class="r"><span>Commune à 25 %% ou plus en QPV</span><div class="tr"><i class="fill" style="--w:%.1f%%;background:#ff5a1f"></i></div><span class="v"><span class="cnt" data-to="%d">0</span> €</span></div></div>'
          % (100.0 * hab0 / hab1, hab0, 100.0, hab1, 100.0 * pa0 / pa1, pa0, 100.0, pa1)
          + '<p class="cap"><b>' + fmt(hab1 / hab0) + " fois plus par habitant, " + fmt(pa1 / pa0) + " fois par personne pauvre</b> : l'écart fond quand on tient compte de la pauvreté.</p>"
          '<p class="src">DGCL via OFGL, dotations ' + str(d["annee"]) + ". Communes de 10 000 habitants et plus.</p></section>")
    s4 = ""
    fod = d.get("fonct")
    if fod:
        ga, gq = fod["groupes"]["aucun"]["hab"], fod["groupes"]["25plus"]["hab"]
        tot = "Dépenses de fonctionnement"
        parts = [("Frais de personnel", "Personnel de la commune", "#ff5a1f"), ("Achats et charges externes", "Achats et charges", "#38bdf8"), ("Dépenses d'intervention", "Subventions et transferts", "#9b6bff"),
                 ("Charges financières", "Intérêts de la dette", "#17c3a2"), ("Autres dépenses de fonctionnement", "Autres", "#6b7480")]
        mxt = max(ga[tot], gq[tot])

        def stack(g, lab, tv):
            segs = "".join('<i style="--w:%.2f%%;background:%s" title="%s : %s"></i>' % (100.0 * g[k] / mxt, c, esc(l), _eur(g[k])) for k, l, c in parts)
            return '<div class="r"><span>%s</span><div class="tr">%s</div><span class="v">%s</span></div>' % (esc(lab), segs, _eur(tv))
        gap, pers = gq[tot] - ga[tot], gq["Frais de personnel"] - ga["Frais de personnel"]
        ann = gq.get("Annuité de la dette", 0) - ga.get("Annuité de la dette", 0)
        eqd = d.get("equip")
        eqs = ""
        if eqd:
            ec = eqd["communes"]["10000"]
            eqs = " En équipement (voirie, bâtiments, écoles), elle dépense " + _eur(ec["25plus"]["eq_hab_an"]) + " par habitant contre " + _eur(ec["aucun"]["eq_hab_an"]) + "."
        s4 = ('<section class="sc"><h2>À quoi sert l\'argent de la commune ?</h2>'
              '<p class="q">Dépenses de fonctionnement par habitant et par an, poste par poste.</p>'
              '<div class="hb stk">' + stack(ga, "Commune sans quartier prioritaire", ga[tot]) + stack(gq, "Commune à 25 % ou plus en QPV", gq[tot]) + "</div>"
              '<div class="slg">' + "".join('<span><i style="background:%s"></i>%s</span>' % (c, esc(l)) for _, l, c in parts) + "</div>"
              '<p class="cap"><b>' + _eur(gap) + " de plus par habitant</b>, dont <b>" + _eur(pers) + " de personnel</b>." + (" Le remboursement de la dette (intérêts et capital) pèse " + _eur(ann) + " de plus." if ann > 0 else "") + eqs
              + " Les chiffres ne disent pas à quoi servent les agents (école, social, sécurité) : on voit la nature de la dépense, pas son usage.</p>"
              '<p class="src">OFGL, comptes des communes, moyenne 2018-2024, budgets principaux.</p></section>')
    s5 = ""
    sc = serv_compare()
    if sc:
        want = [("generaliste", "🩺"), ("dentiste", "🦷"), ("pharmacie", "💊"), ("super", "🛒"), ("banque", "🏦"), ("boulangerie", "🥖"), ("ecole", "🏫"), ("creche", "🧸")]
        by = {r["k"]: r for r in sc["rows"]}
        blocks = []
        for k, em in want:
            r = by.get(k)
            if not r or r["r"] is None:
                continue
            nq, nm = int(round(r["q"])), int(round(r["m"]))
            ic = lambda n, off: "".join('<span style="--i:%d">%s</span>' % (off + i, em) for i in range(n)) or '<span style="--i:0;opacity:.3">∅</span>'
            blocks.append('<div class="ic"><p class="t">%s <span class="meta">(%s)</span></p>'
                          '<div class="ln me"><span>Dans le quartier</span><div class="em">%s</div><b>%s</b></div>'
                          '<div class="ln"><span>Reste de la commune</span><div class="em">%s</div><b>%s</b></div></div>'
                          % (esc(r["lib"]), esc(serv_words(r["r"])), ic(nq, 0), fmt(r["q"]), ic(nm, nq), fmt(r["m"])))
        s5 = ('<section class="sc"><h2>Et dans le quartier lui-même, qu\'y a-t-il ?</h2>'
              '<p class="q">Pour 10 000 habitants, dans le périmètre des quartiers prioritaires, comparé au reste de la même commune.</p>'
              + "".join(blocks) +
              '<p class="cap">Les icônes sont arrondies à l\'unité : le nombre exact est à droite. <b>Moins de dentistes, de banques et de supermarchés, plus d\'écoles et de crèches.</b></p>'
              '<p class="src">Insee, base permanente des équipements ' + str(sc["annee"]) + " (fichier détail géolocalisé). Seuls comptent les équipements dans le périmètre du quartier.</p></section>")
    s6 = ('<section class="sc"><h2>Ce que ces images ne disent pas</h2>'
          '<ul class="no"><li><strong>Ce n\'est pas un habitant « moyen » du quartier.</strong> Ce sont des moyennes de communes, sauf pour les équipements, comptés dans le quartier.</li>'
          "<li><strong>La rénovation urbaine (ANRU)</strong>, les bailleurs sociaux, les écoles, la police, les transports, le RSA et les APL versés ne sont pas dans ces chiffres.</li>"
          "<li><strong>Ce n'est pas une démonstration.</strong> Les dotations de solidarité sont conçues pour cibler les communes en difficulté sociale : un écart par habitant est leur but, pas une anomalie. Reste à savoir s'il suffit.</li></ul>"
          '<a class="more" href="' + BASE_PATH + '/argent-quartiers"><b>Voir la version courte, avec carte</b><span>Les mêmes chiffres en barres, la carte des communes et la conclusion prudente.</span></a>'
          '<a class="more" href="' + BASE_PATH + '/quartiers-prioritaires"><b>Pour aller plus loin : tous les chiffres, les méthodes et les sources</b><span>La page détaillée, avec les tableaux, les évolutions depuis 2019 et les limites.</span></a></section>')
    title = "100 habitants d'un quartier prioritaire : les chiffres en images | ça brûle"
    desc = ("Une animation pour comprendre : sur 100 habitants, combien sont pauvres, combien touchent le RSA, combien l'État verse à la commune, à quoi sert cet argent "
            "et quels services trouve-t-on dans le quartier. Données ouvertes (Insee, CAF, DGCL, OFGL), limites comprises.")
    body = ("<style>" + DEP_CSS + SIMPLE_CSS + HAB_CSS + "</style>"
            "<h1>100 habitants d'un quartier prioritaire</h1>"
            '<p class="lead">Faisons parler les chiffres ouverts avec 100 personnes. Faites défiler : chaque scène s\'anime quand elle apparaît. '
            "On compare des communes de 10 000 habitants ou plus où au moins un quart des habitants vivent en quartier prioritaire (QPV) à celles qui n'en ont aucun.</p>"
            + s1 + s2 + s3 + s4 + s5 + s6 + "<script>" + HAB_JS + "</script>")
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title.split(" | ")[0], "url": url, "inLanguage": "fr-FR", "description": desc,
          "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"}}
    return 200, shell(title, desc, url, body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


OBS_CSS = """
#obs{position:fixed;left:0;right:0;bottom:0;z-index:2000;display:flex;align-items:center;background:#16181d;color:#f2f2f2;
 border-top:2px solid #ff6b35;font:14px/1.3 system-ui,-apple-system,"Segoe UI",sans-serif;height:34px}
#obs .vp{flex:1;overflow:hidden;white-space:nowrap;height:100%;display:flex;align-items:center}
#obs .tk{display:inline-flex;flex:none;animation:obsmv 95s linear infinite}
#obs:hover .tk,#obs:focus-within .tk{animation-play-state:paused}
#obs .tk span{padding-right:4.5em}
#obs a{color:#ffb38f;text-decoration:underline}
#obs b{color:#fff}
#obs button{flex:none;background:none;border:0;color:#f2f2f2;font-size:20px;line-height:1;padding:0 12px;height:100%;cursor:pointer}
@keyframes obsmv{from{transform:translateX(0)}to{transform:translateX(-50%)}}
@media (prefers-reduced-motion:reduce){#obs{height:auto;min-height:34px;padding:6px 0}#obs .vp{white-space:normal;display:block;padding:0 12px}
 #obs .tk{animation:none;display:block}#obs .tk span{padding:0}#obs .tk span+span{display:none}}
body{padding-bottom:36px}
body.obs-off{padding-bottom:0}
body:has(#app){padding-bottom:0}
body:has(#app) #app{height:calc(100dvh - 36px)}
body.obs-off:has(#app) #app{height:100dvh}
"""


def obs_banner():
    """Bandeau défilant : appel à construire un observatoire citoyen (relais syndicats, presse, élus)."""
    one = ('<span><b>Je cherche à fonder un observatoire citoyen</b> de données ouvertes : indépendant, sourcé, sans pub. '
           'Déjà en ligne : <a href="%(b)s/">ça brûle</a> (mobilisations en direct), '
           '<a href="https://oraclemarin.fr/plans-sociaux/">Plans sociaux et faillites</a>, '
           '<a href="%(b)s/argent-quartiers">Argent public et quartiers prioritaires</a>, '
           '<a href="https://oraclemarin.fr/quotas-carbone/">Quotas de CO₂ gratuits de l\'industrie</a>. '
           'Vous connaissez des syndicalistes, des journalistes, des élu·es, des associations ? Mettez-nous en relation : '
           '<a href="mailto:ADRESSE_DE_CONTACT?subject=Observatoire%%20citoyen">ADRESSE_DE_CONTACT</a>. '
           'Un coup de pouce est aussi bienvenu : <a href="https://buymeacoffee.com/capoupascap" rel="noopener" target="_blank">soutenir</a>.</span>') % {"b": BASE_PATH}
    js = ("(function(){try{if(localStorage.getItem('obsoff')>Date.now()){document.getElementById('obs').remove();"
          "document.body.classList.add('obs-off')}}catch(e){}})();"
          "document.getElementById('obsx')&&(document.getElementById('obsx').onclick=function(){var o=document.getElementById('obs');"
          "o&&o.remove();document.body.classList.add('obs-off');try{localStorage.setItem('obsoff',Date.now()+7*864e5)}catch(e){}});")
    return ('<style>%s</style><div id="obs" role="region" aria-label="Appel : observatoire citoyen"><div class="vp"><div class="tk">%s'
            '<span aria-hidden="true">%s</span></div></div><button id="obsx" type="button" aria-label="Fermer le bandeau">&times;</button></div>'
            '<script>%s</script>') % (OBS_CSS, one, one.replace('<a ', '<a tabindex="-1" '), js)


_CAD = {"at": 0.0, "R": None, "busy": False}
_CAD_LOCK = threading.Lock()


def _cadrage_compute():
    """Calcul complet (environ 25 s, lecture seule sur history.db) ; le résultat remplace le cache seulement quand il est prêt."""
    try:
        R = CAD.compute(DB_PATH, before=day_of(time.time()))
        try:
            R["X"] = CAD2.compute_extra(DB_PATH, os.path.join(HERE, "qpv_carte.json"))
        except Exception as e:
            print("cadrage : analyses croisées impossibles", e)
        with _CAD_LOCK:
            _CAD["R"], _CAD["at"] = R, time.time()
    except Exception as e:
        print("cadrage : calcul impossible", e)
    finally:
        with _CAD_LOCK:
            _CAD["busy"] = False


def _cadrage_refresh_async():
    with _CAD_LOCK:
        if _CAD["busy"]:
            return
        _CAD["busy"] = True
    threading.Thread(target=_cadrage_compute, daemon=True).start()


def _cadrage_loop():
    """Préchauffe le cache au démarrage puis le renouvelle toutes les 25 min : aucun visiteur (ni Googlebot) n'attend le calcul."""
    while True:
        _cadrage_refresh_async()
        time.sleep(1500)


def cadrage_data():
    """Chiffres de la page /cadrage : réponse immédiate depuis le cache (même un peu périmé), recalcul en arrière-plan.
    Seul un appel arrivé avant la fin du tout premier calcul après un redémarrage attend (au plus 60 s)."""
    with _CAD_LOCK:
        R, age = _CAD["R"], time.time() - _CAD["at"]
    if R is None:
        _cadrage_refresh_async()
        for _ in range(120):
            time.sleep(0.5)
            with _CAD_LOCK:
                R = _CAD["R"]
            if R is not None:
                break
        return R
    if age > 1800:
        _cadrage_refresh_async()
    return R


_TV = {"mt": 0.0, "D": None}


def tv_data():
    """Comptages franceinfo (tv_data.json, poussé depuis le PC toutes les 3 h) ; rechargé quand le fichier change."""
    path = os.path.join(HERE, "tv_data.json")
    try:
        mt = os.path.getmtime(path)
        if _TV["D"] is None or mt != _TV["mt"]:
            _TV["D"], _TV["mt"] = json.load(open(path, encoding="utf-8")), mt
    except Exception:
        return None
    return _TV["D"]


def tv_route():
    D = tv_data()
    if not D or not D.get("totals"):
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow")
    press = {}
    try:
        with DB_LOCK:
            conn = db()
            try:
                press = {d: n for d, n in conn.execute("SELECT day, articles FROM daily_totals")}
            finally:
                conn.close()
    except Exception:
        pass
    css, body = TVP.build(D, press, esc, BASE_PATH)
    url = BASE_URL + "/franceinfo"
    title = "De quoi parle franceinfo, et avec quels mots ? Sujets du direct | ça brûle"
    desc = ("Les sujets et les champs lexicaux du direct de franceinfo, calculés toutes les trois heures à partir des sous-titres. Seuls des comptages sont publiés ; méthode, limites et licence en clair.")
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title.split(" | ")[0], "url": url, "inLanguage": "fr-FR", "description": desc,
          "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"}, "license": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
          "creator": {"@type": "Person", "name": "Quentin Closset"}}
    return 200, shell(title, desc, url, "<style>" + DEP_CSS + css + "</style>" + body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


def cadrage_page():
    R = cadrage_data()
    if not R or not R["classified"]:
        return 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow")
    css, body = CADP.build(R, esc, BASE_PATH)
    url = BASE_URL + "/cadrage"
    title = "Les mots des titres de presse selon le média, pendant les manifestations | ça brûle"
    desc = ("Pour chaque type de média (nationale, régionale, télés d'info, service public), la part des titres qui parlent de désordre, d'autorités ou de la voix des manifestants. "
            "Explication simple, puis données, méthode et marges d'erreur. Mise à jour chaque jour.")
    ld = {"@context": "https://schema.org", "@type": "WebPage", "name": title.split(" | ")[0], "url": url, "inLanguage": "fr-FR", "description": desc,
          "dateModified": iso_now(), "isPartOf": {"@id": BASE_URL + "/#site"}, "license": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
          "creator": {"@type": "Person", "name": "Quentin Closset"}}
    return 200, shell(title, desc, url, "<style>" + DEP_CSS + css + "</style>" + body, "index,follow", ld, extra_footer=EDITOR_FOOTER)


def seo_route(h):
    path = urllib.parse.urlparse(h.path).path
    code, ctype, body = 200, "text/html; charset=utf-8", None
    noindex = False
    if path in ("/", "/index.html"):
        body = render_index()
    elif path == "/sitemap.xml":
        body, ctype = sitemap_xml(), "application/xml; charset=utf-8"
    elif path.startswith("/ville/"):
        code, body = city_page(path[len("/ville/"):].strip("/"))
    elif path == "/sources":
        code, body = sources_page()
    elif path == "/sources-videos":
        code, body = videos_sources_page()
    elif path == "/lyceens":
        code, body = lyceens_page()
    elif path == "/presse":
        code, body = presse_page()
    elif path == "/integrer":
        code, body = integrer_page()
    elif path == "/sources.json":
        body, ctype = json.dumps(source_registry(), ensure_ascii=False, indent=1), "application/json; charset=utf-8"
    elif path == "/rss.xml" or (path.startswith("/rss/") and path.endswith(".xml") and path[5:-4] in THEME_LABELS):
        body, ctype = rss_xml(path[5:-4] if path != "/rss.xml" else None), "application/rss+xml; charset=utf-8"
    elif path.startswith("/widget/pour/"):
        code, body = recipient_page(path[len("/widget/pour/"):].strip("/"))
        noindex = True
    elif path in ("/widget", "/widget/"):
        code, body, noindex = 404, shell("Page introuvable | ça brûle", "Page introuvable", BASE_URL + "/", "<h1>Page introuvable</h1>", "noindex,nofollow"), True
    elif path == "/widget/manifs":
        q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        body, noindex = manifs_widget_html(q.get("region", ["france"])[0], q.get("theme", ["light"])[0], q.get("map", ["1"])[0] != "0", q.get("only", [""])[0]), True
    elif path.startswith("/widget/") and path[len("/widget/"):] in WIDGET_CFG:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(h.path).query)
        body, noindex = widget_html(path[len("/widget/"):], q.get("theme", ["light"])[0], q.get("map", ["1"])[0] != "0"), True
    elif path in ("/feminicides", "/deces-police", "/accidents-du-travail"):
        code, body = dataset_page(path.strip("/"))
    elif path == "/a-venir":
        code, body = agenda_page()
    elif path == "/departements":
        code, body = departements_page()
    elif path == "/quartiers-prioritaires":
        code, body = qpv_page()
    elif path == "/argent-quartiers":
        code, body = qpv_simple_page()
    elif path == "/habitant-type":
        code, body = habitant_page()
    elif path == "/cadrage":
        code, body = cadrage_page()
    elif path == "/franceinfo":
        code, body = tv_route()
    elif path == "/cadrage.csv":
        R = cadrage_data()
        body, ctype = (CADP.csv_text(R) if R else ""), "text/csv; charset=utf-8"
    elif path.startswith("/departement/"):
        code, body = departement_page(path[len("/departement/"):].strip("/"))
    else:
        return False
    if ctype.startswith("text/html") and not path.startswith("/widget") and "</body>" in body:
        body = body.replace("</body>", obs_banner() + "</body>", 1)
    data = body.encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Cache-Control", "public, max-age=%d" % (60 if path.startswith("/widget/") else 300))
    if noindex:
        h.send_header("X-Robots-Tag", "noindex")
    h.end_headers()
    h.wfile.write(data)
    return True


class H(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml", ".ico": "image/x-icon"}

    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(HERE, "static"), **k)

    def do_GET(self):
        if seo_route(self):
            return
        if self.path.startswith("/api/area"):
            try:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                s_, w_, n_, e_ = (float(q[k][0]) for k in ("south", "west", "north", "east"))
                assert n_ - s_ <= 5 and e_ - w_ <= 8
                z_ = max(0, min(18, int(float(q.get("zoom", ["9"])[0]))))
                out = area(s_, w_, n_, e_, z_)
            except Exception:
                out = {"error": "bad request"}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/search") or self.path.startswith("/api/city"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                if self.path.startswith("/api/search"):
                    out = search(q.get("q", [""])[0][:60])
                else:
                    out = city(q.get("code", [""])[0][:8])
            except Exception:
                out = {"error": "bad request"}
            body = json.dumps(out, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/veille"):
            try:
                d = json.load(open(os.path.join(HERE, "proto", "veille.json"), encoding="utf-8"))
                for v in d.get("videos", []):
                    if v.get("thumb"): v["thumb"] = "vth/" + v["thumb"].split("/")[-1]
                body = json.dumps(d, ensure_ascii=False).encode("utf-8")
            except Exception:
                body = b'{"videos":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Robots-Tag", "noindex")
            self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/api/videos"):
            try:
                d = json.load(open(os.path.join(HERE, "proto", "videos.json"), encoding="utf-8"))
                for v in d.get("videos", []):
                    if v.get("thumb"): v["thumb"] = "vth/" + v["thumb"].split("/")[-1]
                body = json.dumps(d, ensure_ascii=False).encode("utf-8")
            except Exception:
                body = b'{"videos":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers(); self.wfile.write(body)
        elif self.path.startswith("/vth/"):
            n = urllib.parse.urlparse(self.path).path[5:]
            f = os.path.join(HERE, "proto", "th", n)
            if re.fullmatch(r"[0-9a-f]{16}\.jpg", n) and os.path.exists(f):
                b = open(f, "rb").read()
                self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "public, max-age=86400"); self.end_headers(); self.wfile.write(b)
            else:
                self.send_error(404)
        elif self.path.startswith("/api/dataset"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            body = json.dumps(dataset_payload(q.get("name", [""])[0]), ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/go/"):
            gid = urllib.parse.urlparse(self.path).path[4:][:24]
            target = LINKMAP.get(gid) or db_link(gid)
            if target and target.startswith(("http://", "https://")):
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("X-Robots-Tag", "noindex, nofollow")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_error(404)
        elif self.path.startswith("/api/history"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            out = history_day(q["day"][0]) if "day" in q else history_days()
            body = json.dumps(out, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/events"):
            slim = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("v", ["1"])[0] == "2"
            with LOCK:
                st = {**STATE, "throttled_until": BLOCK["until"] if GOOGLE_ON and time.time() < BLOCK["until"] else None}
                body = json.dumps(slim_events(st) if slim else st, ensure_ascii=False, separators=(",", ":") if slim else None).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def end_headers(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/lib/") or p.endswith((".png", ".ico", ".webmanifest")):
            self.send_header("Cache-Control", "public, max-age=604800")
        super().end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    COMMUNES.update(load_communes())
    ALL.extend({(c["nom"], c["dep"]): c for c in COMMUNES.values()}.values())
    print(len(COMMUNES), "communes indexées")
    init_db()
    load_store()
    threading.Thread(target=datasets_loop, daemon=True).start()
    threading.Thread(target=daily_loop, daemon=True).start()
    threading.Thread(target=_cadrage_loop, daemon=True).start()
    if STORE:
        print(len(STORE), "articles rechargés depuis le disque")
        rebuild()
    threading.Thread(target=loop, daemon=True).start()
    print(f"http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
