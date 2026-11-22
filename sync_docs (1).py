"""Synchronise les documents déposés via le formulaire Kobo « Dépôt de documents PCI » (onglet
« Vue d'ensemble » de la page En savoir plus).

Deux étapes, appelées par le workflow .github/workflows/sync_docs.yml :
  python sync_docs.py fetch    télécharge les nouvelles pièces jointes, chiffre les fichiers,
                               signale (need_lo=true) s'il faut LibreOffice pour les aperçus
  python sync_docs.py finish   produit l'aperçu PDF des fichiers Word/PowerPoint/Excel,
                               écrit l'index chiffré docs.enc.json

Sécurité : chaque fichier est chiffré (AES-256-GCM) avec une clé de contenu aléatoire ; cette clé
n'est stockée que dans l'index docs.enc.json, lui-même chiffré avec le code d'accès (PBKDF2-SHA256,
comme data.enc.json). Sans le code, ni la liste ni les fichiers ne sont lisibles.
Un document dont la soumission Kobo est marquée « Non approuvé » (statut de validation) est retiré.

Variables : KOBO_TOKEN (secret), ACCESS_CODE (secret), KOBO_UID_DOCS (variable : UID du formulaire),
KOBO_SERVER (facultatif, https://kf.kobotoolbox.org par défaut).
Dépendances : pip install requests cryptography ; LibreOffice pour les aperçus.
"""
import base64, hashlib, json, os, re, shutil, subprocess, sys, time

IT = 300000
TMP = "_tmp_docs"
STATE = os.path.join(TMP, "state.json")
OFFICE = {"doc", "docx", "odt", "rtf", "ppt", "pptx", "odp", "xls", "xlsx", "ods"}
TYPES = {"compte_rendu": "Compte rendu", "rapport": "Rapport", "plan": "Plan", "note": "Note technique",
         "protocole": "Protocole / procédure", "presentation": "Présentation", "outil": "Outil / fiche",
         "formation": "Support de formation", "autre": "Autre document"}
NOT_APPROVED = "validation_status_not_approved"


def b64(x): return base64.b64encode(x).decode()
def ub64(x): return base64.b64decode(x)


def kdf(code, salt):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=IT).derive(code.encode())


def enc_index(obj, code):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(kdf(code, salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode(), None)
    return {"it": IT, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def dec_index(path, code):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        e = json.load(open(path))
        return json.loads(AESGCM(kdf(code, ub64(e["salt"]))).decrypt(ub64(e["iv"]), ub64(e["ct"]), None))
    except Exception:
        return None


def enc_file(data, key, path):
    """Fichier chiffré : 12 octets d'IV puis le contenu chiffré AES-GCM (lu par index.html)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(iv + AESGCM(key).encrypt(iv, data, None))


def code():
    c = (os.environ.get("ACCESS_CODE") or os.environ.get("ACCES_CODE") or "").strip()
    if not c: sys.exit("ERREUR : secret ACCESS_CODE absent ou vide.")
    return c


def out(k, v):
    p = os.environ.get("GITHUB_OUTPUT")
    if p: open(p, "a").write(f"{k}={v}\n")
    print(f"{k}={v}")


def ext_of(name):
    m = re.search(r"\.([A-Za-z0-9]{1,5})$", name or "")
    return m.group(1).lower() if m else ""


def clean(name):
    name = os.path.basename(str(name or "document"))
    return re.sub(r"[\\/:*?\"<>|]+", "_", name)[:120] or "document"


def fetch():
    import requests
    uid = (os.environ.get("KOBO_UID_DOCS") or "").strip()
    if not uid:
        print("Variable KOBO_UID_DOCS absente : formulaire de dépôt non encore configuré, rien à faire.")
        out("need_lo", "false"); out("skip", "true"); return
    if not os.environ.get("KOBO_TOKEN"): sys.exit("KOBO_TOKEN absent (secret du dépôt).")
    srv = (os.environ.get("KOBO_SERVER") or "https://kf.kobotoolbox.org").rstrip("/")
    h = {"Authorization": "Token " + os.environ["KOBO_TOKEN"]}
    c = code()

    # lien public du formulaire (affiché dans l'onglet « Liens utiles ») et libellés des choix
    LAB = {}
    try:
        a = requests.get(f"{srv}/api/v2/assets/{uid}/", headers=h, timeout=60).json()
        for ch in (a.get("content") or {}).get("choices", []):
            lb = ch.get("label"); lb = lb[0] if isinstance(lb, list) and lb else lb
            if lb: LAB[(ch.get("list_name"), ch.get("name"))] = str(lb)
        links = a.get("deployment__links") or {}
        url = links.get("offline_url") or links.get("url") or ""
        if url:
            json.dump({"url": url, "titre": a.get("name", "")}, open("docs_form.json", "w"), ensure_ascii=False)
    except Exception as e:
        print("Lien du formulaire non récupéré :", e)

    # soumissions
    subs, start = [], 0
    while True:
        r = requests.get(f"{srv}/api/v2/assets/{uid}/data/", headers=h, params={"format": "json", "limit": 1000, "start": start}, timeout=120)
        r.raise_for_status(); j = r.json(); res = j.get("results", [])
        subs += res; start += len(res)
        if not j.get("next") or not res: break
    print(len(subs), "soumission(s) dans le formulaire")

    old = dec_index("docs.enc.json", c) if os.path.exists("docs.enc.json") else None
    if old and old.get("key"):
        key, known = ub64(old["key"]), {d["id"]: d for d in old.get("docs", [])}
    else:  # premier passage, ou code d'accès changé : on repart de zéro avec une nouvelle clé
        print("Nouvel index (première synchronisation ou code d'accès modifié).")
        key, known = os.urandom(32), {}
        shutil.rmtree("docs", ignore_errors=True)

    shutil.rmtree(TMP, ignore_errors=True); os.makedirs(TMP)
    docs, pending = [], []
    for s in subs:
        if ((s.get("_validation_status") or {}).get("uid")) == NOT_APPROVED: continue
        g = lambda k: next((v for kk, v in s.items() if kk == k or kk.endswith("/" + k)), "") or ""
        t = g("type_doc"); tl = TYPES.get(t, t.replace("_", " ").capitalize() or "Document")
        if t == "autre" and g("type_autre"): tl = g("type_autre").strip()[:40]
        atts = [x for x in (s.get("_attachments") or []) if not x.get("is_deleted")]
        for i, at in enumerate(atts):
            fn = clean(at.get("media_file_basename") or at.get("filename") or g("fichier"))
            did = f"{s.get('_id')}-{at.get('uid') or at.get('id') or i}"
            hid = hashlib.sha256(did.encode()).hexdigest()[:16]          # nom de fichier sans information lisible
            meta = {"id": did, "t": t, "tl": tl, "ti": (g("titre") or fn).strip()[:200], "dd": g("date_doc"),
                    "pv": g("province"), "pvl": LAB.get(("provinces", g("province")), ""),
                    "zs": [LAB.get(("zones", z)) or z.split("_", len(g("province").split("_")))[-1].replace("_", " ").title()
                           for z in str(g("zone_sante")).split() if z][:40],
                    "st": g("structure").strip()[:120], "de": g("description").strip()[:600],
                    "fn": fn, "ext": ext_of(fn), "mime": at.get("mimetype") or "", "up": s.get("_submission_time") or "",
                    "f": f"docs/{hid}.bin", "p": None, "sz": 0}
            k = known.get(did)
            reuse = bool(k and k.get("fn") == fn and os.path.exists(k["f"]))
            need_prev = meta["ext"] in OFFICE and not (reuse and k.get("p") and os.path.exists(k["p"]))
            if reuse:                                   # déjà synchronisé : rien à retélécharger
                meta.update({"f": k["f"], "p": k.get("p"), "sz": k.get("sz", 0)})
                if not need_prev: docs.append(meta); continue
            url = at.get("download_url") or at.get("download_large_url")
            if not url: print("Pièce jointe sans lien de téléchargement :", did); continue
            r = requests.get(url, headers=h, timeout=600); r.raise_for_status(); data = r.content
            if not reuse:
                meta["sz"] = len(data); enc_file(data, key, meta["f"])
                print("Nouveau document :", meta["ti"], f"({fn}, {len(data)} octets)")
            if need_prev:                               # aperçu PDF à produire (étape finish)
                open(os.path.join(TMP, hid + "." + meta["ext"]), "wb").write(data); pending.append(did)
            docs.append(meta)
    # fichiers devenus orphelins (soumission supprimée ou non approuvée)
    keep = {d["f"] for d in docs} | {d["p"] for d in docs if d.get("p")}
    for f in (os.listdir("docs") if os.path.isdir("docs") else []):
        if "docs/" + f not in keep: os.remove(os.path.join("docs", f)); print("Retiré :", f)
    json.dump({"key": b64(key), "docs": docs, "pending": pending}, open(STATE, "w"))
    out("need_lo", "true" if pending else "false"); out("skip", "false")


def finish():
    if not os.path.exists(STATE): print("Rien à finaliser."); return
    st = json.load(open(STATE)); key = ub64(st["key"]); docs = st["docs"]; c = code()
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    for d in docs:
        if d["id"] not in st["pending"]: continue
        hid = os.path.basename(d["f"])[:-4]; src = os.path.join(TMP, hid + "." + d["ext"])
        if not os.path.exists(src) or not soffice:
            print("Aperçu non produit pour", d["fn"], "(fichier source ou LibreOffice absent)"); continue
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", TMP, src], check=False, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pdf = os.path.join(TMP, hid + ".pdf")
        if os.path.exists(pdf):
            d["p"] = f"docs/{hid}.p.bin"; enc_file(open(pdf, "rb").read(), key, d["p"]); print("Aperçu PDF :", d["fn"])
        else:
            print("Conversion impossible :", d["fn"])
    docs.sort(key=lambda d: d.get("up") or "", reverse=True)
    idx = {"upd": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "key": st["key"], "docs": docs}
    old = dec_index("docs.enc.json", c) if os.path.exists("docs.enc.json") else None
    if old and {k: v for k, v in old.items() if k != "upd"} == {k: v for k, v in idx.items() if k != "upd"}:
        print("Aucun changement dans la liste des documents.")
    else:
        json.dump(enc_index(idx, c), open("docs.enc.json", "w")); print("docs.enc.json :", len(docs), "document(s)")
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    {"fetch": fetch, "finish": finish}[sys.argv[1] if len(sys.argv) > 1 else "fetch"]()
