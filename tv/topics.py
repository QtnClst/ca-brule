"""Classement des passages de franceinfo : champs lexicaux (listes de /cadrage) et grands sujets (règles de base).

Les sujets sont une base à améliorer : un passage reçoit le sujet qui a le plus de mots reconnus, « Autre sujet » s'il n'en a aucun.
Aucune trace du texte n'est gardée : seuls des comptages sortent de ce module."""
import os
import re
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cadrage as C  # noqa: E402

TOPICS = {
    "mobilisation": ("Manifestations et mobilisation", r"manifest|lycee|lyceen|blocus|greve|syndicat|cortege|mobilisation|intersyndicale|rassemblement|blocage"),
    "international": ("Guerre et international", r"ukrain|russie|poutine|gaza|israel|palestin|hamas|iran|guerre|otan|armee|missile|trump|chine|etats unis|bruxelles|onu|italie|royaume uni|allemagne|union europeenne|diplomat|cessez le feu"),
    "politique": ("Politique française", r"gouvernement|premier ministre|ministre|assemblee|senat|depute|senateur|president de la republique|macron|elysee|matignon|budget|projet de loi|reforme|election|lfi|rassemblement national|republicains|socialiste|motion de censure|parlement|majorite|opposition"),
    "economie": ("Économie et emploi", r"economie|entreprise|inflation|croissance|chomage|emploi|licenci|usine|bourse|pouvoir d achat|banque|dette|deficit|impots?|faillite|salaires?|industrie|commerce|consommat"),
    "justice": ("Justice et faits divers", r"tribunal|proces|condamn|enquete|police|gendarm|garde a vue|interpell|meurtre|agression|accident|incendie|victime|parquet|mis en examen|attentat|fusillade|cambriol|suspect|justice|prison|poignard"),
    "sante": ("Santé", r"sante|hopital|medecin|maladie|virus|epidemie|vaccin|grippe|cancer|soignant|urgences|medicament|patient"),
    "environnement": ("Météo, climat, environnement", r"climat|meteo|tempete|inondation|canicule|pollution|ecolog|energie|nucleaire|agricult|secheresse|neige|vigilance|temperature|pluie|orage|incendies de foret"),
    "sport": ("Sport", r"match|football|rugby|ligue|championnat|coupe du monde|joueur|entraineur|tennis|olympique|psg|equipe de france|transfert|victoire|tournoi|cyclisme"),
    "culture": ("Culture et société", r"film|cinema|festival|livre|musique|concert|artiste|chanteur|exposition|musee|theatre|ecole|education|famille|femmes|religion|logement|retraite|jeunes|enfants|association"),
    "transports": ("Transports", r"sncf|ratp|train|avion|aerien|aeroport|trafic|circulation|metro|route|autoroute|perturb|greve des"),
}
TOPIC_RE = {k: re.compile(v[1]) for k, v in TOPICS.items()}
TOPIC_LABEL = {k: v[0] for k, v in TOPICS.items()}
TOPIC_LABEL["autre"] = "Autre sujet"
TOPIC_LABEL.update({"conflit": "Guerres et conflits (Ukraine…)", "international": "International (hors guerre)"})

# classifieur maison (Bayes naïf entraîné sur ~330 passages étiquetés à la main) ; les poids seuls sont conservés, aucune phrase
CODE = {"CF": "conflit", "IN": "international", "PO": "politique", "MO": "mobilisation", "EC": "economie", "JU": "justice",
        "EN": "environnement", "CU": "culture", "AU": "autre"}
try:
    import nb
    MODEL = nb.load(os.path.join(HERE, "topic_model.json"))
except Exception:
    MODEL = None
MIN_PROB = 0.45


def norm(s):
    s = unicodedata.normalize("NFD", (s or "").lower().replace("’", "'"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[-']", " ", s)


def rule_topic(text):
    t = norm(text)
    best, n_best = "autre", 0
    for k, rx in TOPIC_RE.items():
        n = len(rx.findall(t))
        if n > n_best:
            best, n_best = k, n
    return best, n_best


def topic_of(text):
    """Sujet d'un passage : les règles gardent santé, sport et transports (sans exemples d'entraînement) quand elles sont sûres ;
    sinon le classifieur ; à défaut (probabilité faible), les règles."""
    rt, rn = rule_topic(text)
    if rt in ("sante", "sport", "transports") and rn >= 2:
        return rt
    if MODEL:
        code, prob = nb.predict(MODEL, text)
        if prob >= MIN_PROB:
            return CODE[code]
    return rt


def families_of(text):
    return [f for f, rx in C.FAMRE.items() if rx.search(text or "")]
