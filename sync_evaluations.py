#!/usr/bin/env python3
"""Génère evaluations.enc.json (chiffré) (lu par l'onglet « Évaluations » de l'application) à partir de l'export
KoboToolbox des évaluations PCI (XLSX avec libellés).

Usage :
    ACCESS_CODE=xxxx python sync_evaluations.py --pci export_evaluations.xlsx --sortie evaluations.enc.json
    # ou, téléchargement automatique via l'API Kobo :
    export KOBO_TOKEN=xxxx KOBO_SERVER=https://kf.kobotoolbox.org
    python sync_evaluations.py --uid-pci <valeur de KOBO_UID_ESS> --sortie evaluations.enc.json

Dépendances : pip install pandas openpyxl requests cryptography
Le fichier .enc.json est chiffré avec le code d'accès (variable ACCESS_CODE) : il contient les noms et coordonnées
des établissements et ne s'ouvre qu'avec ce code. Une sortie .json (non chiffrée) reste possible pour un usage local.
"""
import argparse, io, json, os, re, sys, time, warnings
from datetime import datetime, timezone
import pandas as pd
warnings.filterwarnings("ignore")

def pick_lang(srv, h, uid):
    """Choisit la langue française du formulaire (sinon la 1re langue, sinon la langue par défaut)."""
    import requests
    try:
        tr = requests.get(f"{srv}/api/v2/assets/{uid}/?format=json", headers=h).json().get("content", {}).get("translations") or []
    except Exception:
        tr = []
    for t in tr:
        if t and re.search(r"(?i)fran|french|\bfr\b", str(t)): return t
    return tr[0] if tr and tr[0] else "_default"

ES_NAME = "GitHub - evaluations PCI (ne pas supprimer)"

def _sync_export(requests, srv, h, uid, body):
    """Export synchrone Kobo : un « export enregistré » (export-settings) dont le fichier XLSX
    se télécharge directement, sans attendre la file d'exports. Créé automatiquement s'il n'existe pas."""
    base = f"{srv}/api/v2/assets/{uid}/export-settings/"
    es_uid = (os.environ.get("KOBO_ES_ESS") or "").strip()
    if not es_uid:
        r = requests.get(base, headers=h, timeout=60); r.raise_for_status()
        found = [e for e in r.json().get("results", []) if e.get("name") == ES_NAME]
        if found:
            es_uid = found[0]["uid"]
        else:
            cfg = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in body.items()}
            cfg["fields"] = []
            r = requests.post(base, json={"name": ES_NAME, "export_settings": cfg}, headers=h, timeout=60)
            if r.status_code >= 400: raise RuntimeError(f"création de l'export enregistré refusée ({r.status_code}) : {r.text[:300]}")
            es_uid = r.json()["uid"]
            print("Export enregistré créé dans Kobo :", ES_NAME)
    print("Export synchrone Kobo (export enregistré", es_uid + ")…")
    r = requests.get(f"{base}{es_uid}/data.xlsx", headers=h, timeout=1200)
    if r.status_code != 200 or r.content[:2] != b"PK":
        raise RuntimeError(f"réponse inattendue ({r.status_code}) : {r.text[:300] if r.content[:2] != b'PK' else ''}")
    return io.BytesIO(r.content)

def _async_export(requests, srv, h, uid, body):
    """Ancienne méthode : demande d'export puis attente (jusqu'à 25 min). En cas de délai dépassé,
    réutilise l'export terminé le plus récent (données un peu moins fraîches plutôt qu'un échec)."""
    base = f"{srv}/api/v2/assets/{uid}/exports/"
    r = requests.post(base, json=body, headers=h, timeout=60); r.raise_for_status()
    url = r.json()["url"]; j = {}
    for _ in range(300):
        j = requests.get(url, headers=h, timeout=60).json()
        if j.get("status") == "complete": break
        if j.get("status") == "error": raise RuntimeError("Export Kobo en erreur : " + str(j))
        time.sleep(5)
    else:
        print("Délai dépassé pour le nouvel export Kobo : recherche du dernier export terminé…")
        r = requests.get(base, headers=h, params={"limit": 50}, timeout=60); r.raise_for_status()
        done = sorted([e for e in r.json().get("results", []) if e.get("status") == "complete" and e.get("result")],
                      key=lambda e: e.get("date_created", ""), reverse=True)
        if not done: raise RuntimeError("Délai dépassé pour l'export Kobo, et aucun export terminé disponible")
        j = done[0]; print("Utilisation de l'export du", j.get("date_created"))
    data = requests.get(j["result"], headers=h, timeout=600).content
    if j.get("url") == url:
        try: requests.delete(url, headers=h, timeout=60)   # ne pas accumuler les exports dans Kobo
        except Exception: pass
    return io.BytesIO(data)

def kobo_export(uid):
    import requests
    srv = (os.environ.get("KOBO_SERVER") or "https://kf.kobotoolbox.org").rstrip("/")
    if not os.environ.get("KOBO_TOKEN"): sys.exit("KOBO_TOKEN absent (secret du dépôt).")
    if not uid: sys.exit("UID de la base des évaluations absent (variable KOBO_UID_ESS).")
    h = {"Authorization": "Token " + os.environ["KOBO_TOKEN"]}
    lang = os.environ.get("KOBO_LANG") or pick_lang(srv, h, uid)
    print("Langue de l'export Kobo :", lang)
    body = {"type": "xls", "fields_from_all_versions": True, "group_sep": "/", "hierarchy_in_labels": False,
            "multiple_select": "both", "lang": lang}
    try:
        return _sync_export(requests, srv, h, uid, body)
    except Exception as e:
        print("Export synchrone indisponible :", e, "— passage à l'export classique.")
    try:
        return _async_export(requests, srv, h, uid, body)
    except Exception as e:
        sys.exit(str(e))

def _n(t):
    import unicodedata
    t = unicodedata.normalize("NFD", str(t).replace("’", "'").replace("‘", "'")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()

def col(df, *starts, exact=None):
    for c in df.columns:
        s = str(c)
        if (exact and s == exact) or (not exact and any(s.startswith(p) for p in starts)): return c
    # repli : comparaison sans accents, apostrophes ni majuscules, avec ou sans préfixe de groupe
    for c in df.columns:
        n = _n(str(c).split("/")[-1]) if "/" in str(c) else _n(c)
        for p in ([exact] if exact else starts):
            if (exact and n == _n(p)) or (not exact and n.startswith(_n(p))): return c
    print("Colonnes trouvées dans l'export Kobo :")
    for c in list(df.columns)[:40]: print("  -", c)
    sys.exit(f"Colonne introuvable : {exact or starts}")

def build(src):
    a = pd.read_excel(src)
    items = [c for c in a.columns if re.match(r"^\d+\.\d+\s", str(c)) and
             (lambda v: v and v <= {"oui", "non"})(set(a[c].dropna().astype(str).str.lower().unique()))]
    lat = [c for c in a.columns if str(c).endswith("_latitude")][0]; lon = [c for c in a.columns if str(c).endswith("_longitude")][0]
    nm = a[col(a, "Nom de l'établissement de santé")].astype(str).str.strip()
    A = pd.DataFrame({"d": pd.to_datetime(a[col(a, "Date d'évaluation")], errors="coerce"), "sub": a["_submission_time"].astype(str),
        "p": a[col(a, exact="Province")].astype(str).str.strip().str.upper(),
        "z": a[col(a, "Zone de santé")].astype(str).str.replace(" Zone de Santé", "", regex=False).str.strip(),
        "aire": a[col(a, "Aire de santé")].astype(str).str.strip(), "n": nm,
        "aut": a[col(a, exact="Autorité")].astype(str).str.lower().str.strip(), "niv": a[col(a, "Niveau des soins")].astype(str).str.lower().str.strip(),
        "cas": a[col(a, "L'établissement a-t-il signalé un cas d'Ebola")].astype(str).str.lower().str.strip(),
        "sc": a["score_pct"], "lat": a[lat], "lon": a[lon],
        "lt": pd.to_numeric(a[col(a, "Nombre de lits")], errors="coerce"),
        "tw": pd.to_numeric(a[col(a, "Nombre total de travailleurs")], errors="coerce"),
        "ft": a[[c for c in a.columns if re.match(r"^2\.1\s", str(c))][0]].astype(str).str.lower().eq("oui").astype(int)})
    A["aire"] = A["aire"].replace({"nan": "", "None": ""})
    A["bits"] = pd.concat([a[c].astype(str).str.lower().eq("oui").astype(int).astype(str) for c in items], axis=1).agg("".join, axis=1)
    A["key"] = A.p + "|" + A.z + "|" + A["aire"] + "|" + A.n
    A = A.dropna(subset=["d"]).sort_values(["d", "sub"])
    F1 = A.drop_duplicates("key", keep="first").set_index("key"); cnt = A.groupby("key").size(); L = A.drop_duplicates("key", keep="last")
    d0 = pd.Timestamp(A.d.min().date())
    evd = {k: [[int((d - pd.Timestamp(A.d.min().date())).days), round(float(v), 1)] for d, v in zip(g.d, g.sc)] for k, g in A.groupby("key")}
    autm = {"prive": "Privé", "privé": "Privé", "gouvernement": "Étatique", "etatique": "Étatique", "étatique": "Étatique", "conventionne": "Conventionné", "conventionné": "Conventionné"}
    nivm = {"primaire": "Primaire", "secondaire": "Secondaire", "tertiaire": "Tertiaire"}
    P = sorted(L.p.unique()); Z = sorted(L.z.unique()); Au = ["Privé", "Étatique", "Conventionné"]; Nv = ["Primaire", "Secondaire", "Tertiaire"]
    rows = []
    for r in L.itertuples():
        ok = pd.notna(r.lat)
        rows.append([P.index(r.p), Z.index(r.z), Au.index(autm.get(r.aut, "Privé")), Nv.index(nivm.get(r.niv, "Primaire")),
            1 if r.cas == "oui" else 0, round(float(r.sc), 1), int((r.d - d0).days), r.bits, round(float(F1.sc[r.key]), 1), int(cnt[r.key]),
            round(float(r.lat), 3) if ok else None, round(float(r.lon), 3) if ok else None, r.n[:40], F1.bits[r.key] if cnt[r.key] > 1 else "",
            r.aire,
            None if pd.isna(r.lt) else int(r.lt), None if pd.isna(r.tw) else int(r.tw), int(r.ft), evd[r.key]])
    q = {"rows": len(A), "ess": len(L), "dups": len(A) - len(L), "nogps": int(L.lat.isna().sum()),
         "gpsout": int((L.lat.notna() & ((L.lat.abs() > 14) | (L.lon < 11) | (L.lon > 32))).sum())}
    return {"upd": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), "P": P, "Z": Z, "Au": Au, "Nv": Nv, "rows": rows,
            "th": [int(re.match(r"^(\d+)\.", c).group(1)) for c in items], "lab": [re.sub(r"\s+", " ", c) for c in items], "q": q, "d0": str(d0.date())}

# ---------- chiffrement (identique à sync_kobo.py et à la fonction dec() de index.html) ----------
IT = 300000
def _kdf(code, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=IT).derive(code.encode())

def encrypt(data: bytes, code: str) -> dict:
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    b = lambda x: base64.b64encode(x).decode()
    return {"it": IT, "salt": b(salt), "iv": b(iv), "ct": b(AESGCM(_kdf(code, salt)).encrypt(iv, data, None))}

def strip_upd(txt):
    """Contenu sans la date de génération, pour ne republier que si les données ont changé."""
    try:
        j = json.loads(txt); j.pop("upd", None); return json.dumps(j, sort_keys=True, ensure_ascii=False)
    except Exception:
        return None

def decrypt_existing(path, code):
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        e = json.load(open(path)); b = base64.b64decode
        return strip_upd(AESGCM(_kdf(code, b(e["salt"]))).decrypt(b(e["iv"]), b(e["ct"]), None).decode("utf8"))
    except Exception:
        return None

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--pci"); ap.add_argument("--uid-pci"); ap.add_argument("--sortie", default="evaluations.enc.json")
    g = ap.parse_args()
    src = g.pci or (kobo_export(g.uid_pci) if g.uid_pci else sys.exit("Indiquez --pci ou --uid-pci"))
    out = build(src)
    if g.sortie.endswith(".enc.json"):
        # Fichier chiffré (AES-256-GCM, clé PBKDF2-SHA256) avec le même code d'accès que les Sitreps.
        code = (os.environ.get("ACCESS_CODE") or os.environ.get("ACCES_CODE") or "").strip()
        if not code: sys.exit("ERREUR : secret ACCESS_CODE absent ou vide (Settings > Secrets and variables > Actions).")
        data = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
        if decrypt_existing(g.sortie, code) == strip_upd(data):
            print("Aucun changement : rien à publier"); sys.exit(0)
        json.dump(encrypt(data.encode(), code), open(g.sortie, "w"))
    else:
        json.dump(out, open(g.sortie, "w", encoding="utf8"), ensure_ascii=False, separators=(",", ":"))
    print(f"OK : {g.sortie} — {out['q']['ess']} établissements, {out['q']['rows']} évaluations")
