"""
Import unique des PPL du classeur Excel (11/11/2026) dans le formulaire Kobo PPL.

ppl_import.enc contient les fiches (noms compris) chiffrées avec une clé aléatoire : le fichier est
illisible sans le secret PPL_IMPORT_KEY. Après un import réussi, le workflow supprime ce fichier du dépôt.

Variables : KOBO_TOKEN, PPL_IMPORT_KEY (secrets), KOBO_UID_PPL (variable), KOBO_SERVER (facultatif).
Relancer l'import ne crée pas de doublons : Kobo ignore une fiche strictement identique à une fiche déjà reçue.
"""
import base64, gzip, io, json, os, sys, time
from xml.sax.saxutils import escape
import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SRC = "ppl_import.enc"


def kc_hosts(srv):
    """Adresses possibles de l'API de soumission (KoboToolbox l'a déplacée selon les versions)."""
    h = [srv]
    if "://kf." in srv: h.insert(0, srv.replace("://kf.", "://kc."))
    if "://eu." in srv: h.insert(0, srv.replace("://eu.", "://kc-eu."))
    return h


def main():
    for k in ("KOBO_TOKEN", "PPL_IMPORT_KEY", "KOBO_UID_PPL"):
        if not (os.environ.get(k) or "").strip():
            sys.exit(f"{k} absent : voir les instructions d'installation du formulaire PPL.")
    if not os.path.exists(SRC):
        print("ppl_import.enc absent : import déjà réalisé, rien à faire."); return
    raw = open(SRC, "rb").read()
    try:
        recs = json.loads(gzip.decompress(AESGCM(base64.b64decode(os.environ["PPL_IMPORT_KEY"].strip())).decrypt(raw[:12], raw[12:], None)))
    except Exception:
        sys.exit("Clé PPL_IMPORT_KEY incorrecte : recopiez-la exactement (sans espace).")
    srv = (os.environ.get("KOBO_SERVER") or "https://kf.kobotoolbox.org").rstrip("/")
    uid, h = os.environ["KOBO_UID_PPL"].strip(), {"Authorization": "Token " + os.environ["KOBO_TOKEN"].strip()}
    a = requests.get(f"{srv}/api/v2/assets/{uid}/", headers=h, timeout=60)
    a.raise_for_status(); a = a.json()
    ver = a.get("deployed_version_id") or a.get("version_id") or ""
    if not a.get("has_deployment", True):
        sys.exit("Le formulaire PPL n'est pas encore déployé dans Kobo : cliquez sur « Déployer » puis relancez.")
    fuuid = ""
    for host in kc_hosts(srv):                                   # identifiant interne du formulaire (facultatif)
        try:
            r = requests.get(f"{host}/api/v1/forms", headers=h, params={"id_string": uid}, timeout=60)
            if r.ok and isinstance(r.json(), list) and r.json():
                fuuid = r.json()[0].get("uuid") or ""; break
        except Exception:
            pass

    def xml(rec):
        groups = {}
        for k, v in rec.items():
            if k.startswith("_"): continue
            g, n = k.split("/")
            groups.setdefault(g, []).append(f"<{n}>{escape(str(v))}</{n}>")
        body = "".join(f"<{g}>{''.join(x)}</{g}>" for g, x in groups.items())
        fh = f"<formhub><uuid>{fuuid}</uuid></formhub>" if fuuid else ""
        return (f'<?xml version="1.0" ?><{uid} id="{uid}" version="{ver}">{fh}{body}'
                f'<__version__>{ver}</__version__><meta><instanceID>{rec["_iid"]}</instanceID></meta></{uid}>').encode()

    url, ok, dup, err = None, 0, 0, []
    for i, rec in enumerate(recs):
        files = lambda: {"xml_submission_file": ("submission.xml", io.BytesIO(xml(rec)), "text/xml")}
        cand = [url] if url else [f"{x}/api/v1/submissions" for x in kc_hosts(srv)] + [f"{x}/submission" for x in kc_hosts(srv)]
        res = None
        for u in cand:
            for t in range(3):
                try:
                    res = requests.post(u, headers={**h, "X-OpenRosa-Version": "1.0"}, files=files(), timeout=60)
                    break
                except requests.RequestException:
                    time.sleep(3 * (t + 1))
            if res is not None and res.status_code in (200, 201, 202):
                url = u; break
        if res is not None and res.status_code in (200, 201, 202):
            if "duplicate" in res.text.lower(): dup += 1
            else: ok += 1
        else:
            err.append(f"fiche {i + 1} : HTTP {res.status_code if res is not None else '—'} {res.text[:200] if res is not None else ''}")
            if len(err) >= 5 and ok == 0:
                break                                            # rien ne passe : inutile d'insister
        if (i + 1) % 50 == 0: print(f"{i + 1}/{len(recs)}…")
    print(f"Import terminé : {ok} fiche(s) créée(s), {dup} déjà présente(s), {len(err)} erreur(s). Adresse utilisée : {url}")
    for e in err[:10]: print(" ", e)
    if err:
        sys.exit("Import incomplet : ppl_import.enc est conservé pour pouvoir relancer.")
    open(".import_ok", "w").write("ok")


if __name__ == "__main__":
    main()
