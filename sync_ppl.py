"""
Synchronisation des PPL infectés (formulaire Kobo « Base PPL infectés – MVE 17 ») → ppl.enc.json

Les noms, ID EPID, N° de base centrale et observations ne quittent JAMAIS Kobo : seules les variables
utiles aux visuels sont publiées, puis chiffrées (AES-256-GCM, clé dérivée du code d'accès).

Variables : KOBO_TOKEN (secret), ACCESS_CODE (secret) ; KOBO_UID_PPL et KOBO_SERVER facultatifs
(le formulaire est retrouvé grâce à son lien Enketo ZTKeReRt).

Test local à partir du classeur Excel :  python sync_ppl.py --xlsx BDD_PPL.xlsx
"""
import base64, datetime, gzip, json, os, re, sys, unicodedata

IT = 300000
OUT = "ppl.enc.json"
TS = ["ESS", "CTE", "Autre site de riposte", "Non précisé"]
INV = ["Investigué", "Non investigué", "Suspect"]
ET = ["Actif", "Vivant", "Guéri", "Décédé", "Transféré", "Évadé", "Non-cas"]
# profil de chaque catégorie professionnelle : sd = soins directs, hs = hors soins directs (appui riposte), ap = à préciser
PROFIL = {"Aide-soignant(e)": "sd", "Anesthésiste": "sd", "Auxiliaire de santé": "sd", "Directeur de nursing": "sd", "Infirmier(ère)": "sd",
          "Médecin": "sd", "Nutritionniste": "sd", "Orthopédiste": "sd", "Pharmacien(ne)": "sd", "Sage-femme / Accoucheuse": "sd",
          "Technicien de laboratoire": "sd", "Tradipraticien": "sd", "ACZ (administratif zone de santé)": "hs", "AG (administrateur gestionnaire)": "hs",
          "APS (agent psychosocial)": "hs", "Agent de communication / mobilisation sociale": "hs", "Agent de sécurité": "hs",
          "Agent point de contrôle": "hs", "Brancardier": "hs", "Chauffeur": "hs", "Comptable": "hs", "Fille de salle": "hs", "Hygiéniste": "hs",
          "Logisticien": "hs", "Réceptionniste": "hs", "Technicien de surface": "hs", "Équipe EDS (inhumation digne et sécurisée)": "hs",
          "Équipe EDS (inhumation digne et sécurisé)": "hs", "Agent de santé communautaire (ASC)": "ap", "Non précisé (personnel de santé / PPL)": "ap"}

sl = lambda s: re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFD", str(s or "")).encode("ascii", "ignore").decode().lower()).strip("_")


# ---------------------------------------------------------------- chiffrement (même format que les autres fichiers)
def kdf(code, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=IT).derive(code.encode())


def kdf_salt():
    """Sel commun à tous les fichiers chiffrés (kdf.json, public) : le navigateur ne calcule la clé qu'une fois."""
    try:
        return base64.b64decode(json.load(open("kdf.json"))["salt"])
    except Exception:
        return os.urandom(16)


def encrypt(obj, code):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv, b = kdf_salt(), os.urandom(12), lambda x: base64.b64encode(x).decode()
    raw = gzip.compress(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), 9)
    return {"it": IT, "salt": b(salt), "iv": b(iv), "z": "gz", "ct": b(AESGCM(kdf(code, salt)).encrypt(iv, raw, None))}


def decrypt_existing(code):
    """Contenu actuellement publié (None s'il est absent, illisible ou à l'ancien format)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        e, b = json.load(open(OUT)), base64.b64decode
        if e.get("z") != "gz" or (os.path.exists("kdf.json") and b(e["salt"]) != kdf_salt()):
            return None
        return json.loads(gzip.decompress(AESGCM(kdf(code, b(e["salt"]))).decrypt(b(e["iv"]), b(e["ct"]), None)))
    except Exception:
        return None


def code():
    c = (os.environ.get("ACCESS_CODE") or os.environ.get("ACCES_CODE") or "").strip()
    if not c:
        sys.exit("Code d'accès absent : créez le secret ACCESS_CODE (Settings → Secrets and variables → Actions).")
    return c


# ---------------------------------------------------------------- mise en forme commune (Kobo ou classeur Excel)
def build(recs):
    """recs : dicts {p, z, a, e, ts, dc, age, sx, cat, inv, et, de, key}. Renvoie l'objet publié (sans aucun nom)."""
    P, Z, A, E, C = [], [], [], [], []
    ix = lambda L, v: (L.index(v) if v in L else (L.append(v) or len(L) - 1)) if v else -1
    seen, rows = {}, []
    for r in recs:
        if r.get("key"):
            seen[r["key"]] = seen.get(r["key"], 0) + 1
    for r in sorted(recs, key=lambda r: (r.get("dc") or "9999", r.get("z") or "")):
        age = r.get("age")
        try:
            age = int(float(age)) if age not in (None, "") else -1
        except ValueError:
            age = -1
        rows.append([ix(P, r.get("p")), ix(Z, r.get("z")), ix(A, r.get("a")), ix(E, r.get("e")),
                     TS.index(r["ts"]) if r.get("ts") in TS else -1, r.get("dc") or "", age, r.get("sx") or "",
                     ix(C, r.get("cat")), INV.index(r["inv"]) if r.get("inv") in INV else -1,
                     ET.index(r["et"]) if r.get("et") in ET else -1, r.get("de") or "",
                     1 if r.get("key") and seen[r["key"]] > 1 else 0])
    return {"P": P, "Z": Z, "A": A, "E": E, "C": [[c, PROFIL.get(c, "ap")] for c in C], "TS": TS, "INV": INV, "ET": ET,
            "cols": ["province", "zone", "aire", "structure", "type_structure", "date_confirmation", "age", "sexe",
                     "categorie", "investigation", "etat", "date_etat", "doublon"], "rows": rows}


ENKETO_PPL = "ZTKeReRt"   # lien du formulaire PPL : https://ee.kobotoolbox.org/x/ZTKeReRt


def find_uid(srv, h):
    """UID du formulaire PPL : variable KOBO_UID_PPL si elle existe, sinon recherche du formulaire
    dont le lien Enketo est ENKETO_PPL (ou la variable KOBO_ENKETO_PPL) parmi les projets du compte."""
    import requests
    uid = (os.environ.get("KOBO_UID_PPL") or "").strip()
    if uid:
        return uid
    ek = (os.environ.get("KOBO_ENKETO_PPL") or ENKETO_PPL).strip().rstrip("/").split("/")[-1]
    url, seen = f"{srv}/api/v2/assets/", []
    params = {"q": "asset_type:survey", "limit": 200, "format": "json"}
    while url:
        r = requests.get(url, headers=h, params=params, timeout=60); r.raise_for_status(); j = r.json(); params = None
        seen += j.get("results", []); url = j.get("next")
    for a in seen:
        if ek in json.dumps(a.get("deployment__links") or {}):
            return a["uid"]
    for a in seen:                                        # liens absents de la liste : on lit chaque projet déployé
        if a.get("has_deployment") or a.get("deployment__active"):
            d = requests.get(f"{srv}/api/v2/assets/{a['uid']}/", headers=h, params={"format": "json"}, timeout=60)
            if d.ok and ek in json.dumps(d.json().get("deployment__links") or {}):
                return a["uid"]
    sys.exit(f"Formulaire au lien Enketo « {ek} » introuvable avec ce KOBO_TOKEN : vérifiez qu'il est déployé "
             f"et qu'il appartient (ou est partagé) au compte du jeton. Projets lus : {len(seen)}.")


def nice(s):
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s.upper() if s else ""


# ---------------------------------------------------------------- source 1 : Kobo
def from_kobo():
    import requests
    if not os.environ.get("KOBO_TOKEN"):
        sys.exit("KOBO_TOKEN absent (secret du dépôt).")
    srv = (os.environ.get("KOBO_SERVER") or "https://kf.kobotoolbox.org").rstrip("/")
    h = {"Authorization": "Token " + os.environ["KOBO_TOKEN"].strip()}
    uid = find_uid(srv, h)
    a = requests.get(f"{srv}/api/v2/assets/{uid}/", headers=h, timeout=60)
    a.raise_for_status()
    lab = {}
    for c in (a.json().get("content") or {}).get("choices", []):
        l = c.get("label")
        lab[(c.get("list_name"), c.get("name"))] = (l[0] if isinstance(l, list) and l else l) or c.get("name")
    subs, start = [], 0
    while True:
        r = requests.get(f"{srv}/api/v2/assets/{uid}/data/", headers=h, params={"format": "json", "limit": 1000, "start": start}, timeout=120)
        r.raise_for_status()
        j = r.json(); subs += j.get("results", [])
        if not j.get("next"): break
        start += 1000
    print(len(subs), "fiche(s) PPL dans Kobo")
    recs = []
    for s in subs:
        if (s.get("_validation_status") or {}).get("uid") == "validation_status_not_approved":
            continue                                          # fiches rejetées dans Kobo : exclues
        g = lambda k: next((v for kk, v in s.items() if kk == k or kk.endswith("/" + k)), None)
        L = lambda lst, k: lab.get((lst, g(k)), g(k)) if g(k) else None
        cat = L("categorie", "categorie")
        if g("categorie") == "autre":
            cat = (g("categorie_autre") or "Autre / non renseigné").strip()
        recs.append({"p": nice(L("provinces", "province")), "z": nice(L("zones", "zone")), "a": nice(g("aire")), "e": nice(g("structure")),
                     "ts": {"ess": "ESS", "cte": "CTE", "autre_site": "Autre site de riposte", "non_precise": "Non précisé"}.get(g("type_structure")),
                     "dc": (g("date_confirmation") or "")[:10], "age": g("age"), "sx": g("sexe") if g("sexe") in ("M", "F") else "",
                     "cat": cat, "inv": {"investigue": "Investigué", "non_investigue": "Non investigué", "suspect": "Suspect"}.get(g("investigation")),
                     "et": {"actif": "Actif", "vivant": "Vivant", "gueri": "Guéri", "decede": "Décédé", "transfere": "Transféré", "evade": "Évadé", "non_cas": "Non-cas"}.get(g("etat")),
                     "de": (g("date_etat") or "")[:10], "key": sl(g("nom")) + "|" + sl(g("zone")) if g("nom") else ""})
    return recs


# ---------------------------------------------------------------- source 2 : classeur Excel (tests, sans publication des noms)
def from_xlsx(path):
    import openpyxl
    wb, recs = openpyxl.load_workbook(path, data_only=True), []
    E2 = {"ACTIF": "Actif", "VIVANT": "Vivant", "GUERI": "Guéri", "DECEDE": "Décédé", "TRANSFERE": "Transféré", "EVADE": "Évadé", "NON-CAS": "Non-cas"}
    I2 = {"INVESTIGUE": "Investigué", "NON INVESTIGUE": "Non investigué", "SUSPECT": "Suspect"}
    d = lambda v: v.strftime("%Y-%m-%d") if isinstance(v, (datetime.date, datetime.datetime)) else ""
    for ws in wb.worksheets:
        if ws["A7"].value != "N°" or ws.title == "MODELE_ZS":
            continue
        for r in ws.iter_rows(min_row=8, max_col=17, values_only=True):
            if not r[2]:
                continue
            recs.append({"p": nice(ws["D2"].value), "z": nice(ws["F2"].value), "a": nice(r[7]), "e": nice(r[8]),
                         "ts": r[9] if r[9] in TS else None, "dc": d(r[10]), "age": r[4], "sx": r[5] if r[5] in ("M", "F") else "",
                         "cat": r[11], "inv": I2.get(r[14]), "et": E2.get(r[15]), "de": "", "key": sl(r[2]) + "|" + sl(ws["F2"].value)})
    return recs


def main():
    if "--xlsx" in sys.argv:
        recs = from_xlsx(sys.argv[sys.argv.index("--xlsx") + 1])
    else:
        recs = from_kobo()
        if recs is None:
            return
    c = code()
    obj = build(recs)
    old = decrypt_existing(c)
    if old and {k: v for k, v in old.items() if k != "upd"} == obj:
        print("Aucun changement : rien à publier."); return
    obj["upd"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    json.dump(encrypt(obj, c), open(OUT, "w"))
    print(f"{OUT} publié : {len(obj['rows'])} PPL, {len(obj['Z'])} zones de santé.")


if __name__ == "__main__":
    main()
