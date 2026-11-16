"""Appariement des noms de candidats dans un passage de sous-titres (en mémoire ; seuls les comptages sont enregistrés)."""
import json, os, re, unicodedata
_D = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "noms.json"), encoding="utf-8"))
_RX = [(c["nom"], [re.compile(m) for m in c["motifs"]]) for c in _D["candidats"]]
NOMS = [c["nom"] for c in _D["candidats"]]
def norm(t):
    return unicodedata.normalize("NFD", t.lower()).encode("ascii", "ignore").decode()
def match(text):
    """Liste des candidats cités au moins une fois dans le passage."""
    t = norm(text)
    return [n for n, rxs in _RX if any(r.search(t) for r in rxs)]

_CX = [(c["id"], [re.compile(m) for m in c["motifs"]]) for c in _D.get("contextes", [])]
def contexts(text):
    """Identifiants des listes de mots clés présentes dans le passage (même règle pour tous les candidats)."""
    t = norm(text)
    return [i for i, rxs in _CX if any(r.search(t) for r in rxs)]
