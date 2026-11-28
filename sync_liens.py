"""
Liens qui alimentent l'application → liens.json (public : aucun identifiant, aucune donnée)

Chaque formulaire Kobo relié à l'application (variables du dépôt KOBO_UID, KOBO_UID_ESS, KOBO_UID_DOCS,
KOBO_UID_PPL… et toute nouvelle variable KOBO_UID_xxx) apparaît automatiquement dans
Docs & Liens › Liens utiles, avec son lien public (Enketo). Une autre source (site, dépôt de données…)
s'ajoute avec une variable LIEN_xxx de la forme :  Titre | https://adresse | ce qu'elle alimente

Variables : KOBO_TOKEN (secret), VARS_JSON (toutes les variables du dépôt, fournies par le workflow).
"""
import datetime, json, os, re, sys

OUT = "liens.json"
SRV = (os.environ.get("KOBO_SERVER") or "https://kf.kobotoolbox.org").rstrip("/")
# formulaires connus : variable → (identifiant, titre du bouton, ce qu'il alimente, délai de synchronisation)
KNOWN = {
    "KOBO_UID": ("sitreps", "Soumettre votre Sitrep", "Sitreps", "environ 5 minutes"),
    "KOBO_UID_ESS": ("evaluations", "Effectuer une Évaluation PCI (EBOD RAT)", "Évaluations › EBOD RAT", "quelques heures"),
    "KOBO_UID_DOCS": ("documents", "Déposer un document (compte rendu, rapport, plan…)", "Docs & Liens › Documents", "environ une heure"),
    "KOBO_UID_PPL": ("ppl", "Déclarer ou mettre à jour un PPL infecté", "Évaluations › PPL", "environ une heure"),
}
ORDER = ["sitreps", "evaluations", "ppl", "documents"]
ENKETO_PPL = "ZTKeReRt"           # formulaire PPL retrouvé par son lien si KOBO_UID_PPL n'est pas défini
EPI = {"id": "epidemio", "type": "web", "titre": "Données épidémiologiques INSP / INRB (dépôt public)",
       "url": "https://github.com/INRB-UMIE/BDBV2026-Data", "alimente": "Épidémiologie", "delai": "toutes les 6 heures",
       "note": "Source publique en lecture seule : rien à saisir."}


def kobo(uid, h):
    import requests
    r = requests.get(f"{SRV}/api/v2/assets/{uid}/", headers=h, params={"format": "json"}, timeout=60)
    r.raise_for_status()
    return r.json()


def enketo(a):
    L = a.get("deployment__links") or {}
    for k in ("offline_url", "url", "single_url", "single_once_url"):
        if L.get(k):
            return L[k]
    return ""


def find_by_enketo(ek, h):
    import requests
    url, params = f"{SRV}/api/v2/assets/", {"q": "asset_type:survey", "limit": 200, "format": "json"}
    while url:
        r = requests.get(url, headers=h, params=params, timeout=60); r.raise_for_status(); j = r.json(); params = None
        for a in j.get("results", []):
            if ek in json.dumps(a.get("deployment__links") or {}):
                return a["uid"]
            if a.get("has_deployment") and "deployment__links" not in a:
                try:
                    if ek in json.dumps(kobo(a["uid"], h).get("deployment__links") or {}):
                        return a["uid"]
                except Exception:
                    pass
        url = j.get("next")
    return ""


def main():
    try:
        V = json.loads(os.environ.get("VARS_JSON") or "{}")
    except ValueError:
        V = {}
    for k in KNOWN:                                    # variables passées individuellement (tests, anciens workflows)
        if os.environ.get(k) and not V.get(k):
            V[k] = os.environ[k]
    tok = (os.environ.get("KOBO_TOKEN") or "").strip()
    if not tok:
        sys.exit("KOBO_TOKEN absent (secret du dépôt).")
    h = {"Authorization": "Token " + tok}
    if not V.get("KOBO_UID_PPL"):
        try:
            V["KOBO_UID_PPL"] = find_by_enketo(ENKETO_PPL, h)
        except Exception as e:
            print("Formulaire PPL non retrouvé :", e)
    out = []
    for var in sorted(k for k in V if re.fullmatch(r"KOBO_UID(_[A-Z0-9]+)?", k) and V[k]):
        uid = str(V[var]).strip()
        ident, titre, alim, delai = KNOWN.get(var, (var[9:].lower() or "kobo", "", "Nouveau formulaire relié à l’application", "selon son workflow"))
        try:
            a = kobo(uid, h)
        except Exception as e:
            print(f"{var} : formulaire illisible ({e}) — ignoré."); continue
        url = enketo(a)
        if not url or not a.get("deployment__active", a.get("has_deployment", True)):
            print(f"{var} : formulaire non déployé — ignoré."); continue
        out.append({"id": ident, "type": "kobo", "titre": titre or a.get("name") or var, "formulaire": a.get("name") or "",
                    "url": url, "alimente": alim, "delai": delai, "fiches": a.get("deployment__submission_count")})
        print(f"{var} → {out[-1]['titre']} : {url}")
    out.sort(key=lambda x: (ORDER.index(x["id"]) if x["id"] in ORDER else 99, x["titre"]))
    out.append(EPI)
    for var in sorted(k for k in V if k.startswith("LIEN_")):
        p = [x.strip() for x in str(V[var]).split("|")]
        if len(p) >= 2 and re.match(r"https?://", p[1]):
            out.append({"id": var[5:].lower(), "type": "web", "titre": p[0], "url": p[1], "alimente": p[2] if len(p) > 2 else "", "delai": ""})
            print(f"{var} → {p[0]} : {p[1]}")
        else:
            print(f"{var} ignorée : format attendu « Titre | https://adresse | ce qu'elle alimente ».")
    obj = {"liens": out}
    try:
        old = json.load(open(OUT))
        if {k: v for k, v in old.items() if k != "upd"} == obj:
            print("Aucun changement."); return
    except Exception:
        pass
    obj["upd"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    json.dump(obj, open(OUT, "w"), ensure_ascii=False, indent=1)
    print(f"{OUT} publié : {len(out)} lien(s).")


if __name__ == "__main__":
    main()
