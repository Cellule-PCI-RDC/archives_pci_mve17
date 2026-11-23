# Construit epi.json (page « Épidémiologie ») à partir du dépôt public INRB-UMIE/BDBV2026-Data.
# Usage : python build_epidemio.py <dossier build/ du dépôt INRB> <data/aliases.csv> epi.json
# Dépendance : pip install shapely
import csv, json, re, sys, datetime as dt, unicodedata
from shapely.geometry import shape, mapping
B = sys.argv[1]  # build dir
L = B + '/long/'
alias = {}
for r in csv.DictReader(open(sys.argv[2], encoding='utf-8-sig')):
    alias[r[list(r)[0]].strip()] = r[list(r)[1]].strip()
g = json.load(open(B + '/drc_health_zones.geojson', encoding='utf-8'))
norm = lambda s: re.sub(r'[^a-z]', '', unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode().lower())
canon = {norm(f['properties']['nom']): f['properties']['nom'] for f in g['features']}
def resolve(z):
    z = alias.get(z, z)
    return canon.get(norm(z))
def num(v):
    m = re.match(r'^\s*(\d+)', v or '')
    return int(m.group(1)) if m else None
def series(fn):
    out = {}
    unk = set()
    for row in csv.reader(open(L + fn, encoding='utf-8-sig')):
        if len(row) < 3: continue
        z, d, v = row[0].strip(), re.sub(r'[^0-9-]', '', row[1]), num(row[2])
        c = resolve(z) if z != 'DRC' else 'DRC'
        if not c: unk.add(z); continue
        if v is None: continue
        out.setdefault(c, {})[d] = max(v, out.get(c, {}).get(d, 0))
    return out, unk
cc, u1 = series('insp_sitrep__cumulative_confirmed_cases.csv')
cd, u2 = series('insp_sitrep__cumulative_confirmed_deaths.csv')
print('unmatched', u1 | u2, file=sys.stderr)
nat = {k: series('insp_sitrep__national_cumulative_' + k + '.csv')[0]['DRC'] for k in ['confirmed_cases', 'confirmed_deaths', 'recovered_cases']}
last = max(nat['confirmed_cases'])
D = lambda s: dt.date.fromisoformat(s)
def at(sr, day):  # latest value on or before day
    ks = [k for k in sr if D(k) <= day]
    return sr[max(ks)] if ks else 0
props = {f['properties']['nom']: f['properties'] for f in g['features']}
zones = []
for z, sr in cc.items():
    ld = max(sr); now = sr[ld]
    if now <= 0: continue
    lastday = D(last)
    dsr = cd.get(z, {})
    zones.append(dict(z=z, p=props[z]['province'], c=now,
        d=dsr[max(dsr)] if dsr else None,
        c21=max(0, now - at(sr, lastday - dt.timedelta(days=21))),
        c7=max(0, now - at(sr, lastday - dt.timedelta(days=7))),
        first=min(k for k, v in sr.items() if v > 0), upd=ld,
        pop=round((props[z].get('worldpop', {}).get('pop_count', {}) or {}).get('pop_count') or 0)))
zones.sort(key=lambda r: -r['c'])
def weekly(sr):
    ks = sorted(sr); w = {}; prev = 0
    for k in ks:
        inc = max(0, sr[k] - prev); prev = max(prev, sr[k])
        d = D(k); mon = (d - dt.timedelta(days=d.weekday())).isoformat()
        w[mon] = w.get(mon, 0) + inc
    start = D(min(w)); end = D(max(w)); out = []
    while start <= end:
        out.append([start.isoformat(), w.get(start.isoformat(), 0)]); start += dt.timedelta(days=7)
    return out
wc, wd = weekly(nat['confirmed_cases']), weekly(nat['confirmed_deaths'])
# filtre « semaine épidémiologique » : pour chaque zone et chaque semaine (lundi → dimanche),
# [nouveaux cas, nouveaux décès, cas cumulés au dimanche, décès cumulés au dimanche]
WEEKS = [w[0] for w in wc]
for zr in zones:
    sr, dsr = cc[zr['z']], cd.get(zr['z'], {})
    nw = dict(weekly(sr)); nd = dict(weekly(dsr)) if dsr else {}
    zr['w'] = [[nw.get(m, 0), nd.get(m, 0), at(sr, D(m) + dt.timedelta(days=6)), at(dsr, D(m) + dt.timedelta(days=6)) if dsr else 0] for m in WEEKS]
cum = [[k, nat['confirmed_cases'][k], nat['confirmed_deaths'].get(k), nat['recovered_cases'].get(k)] for k in sorted(nat['confirmed_cases'])]
feats = []
for f in g['features']:
    s = shape(f['geometry']).simplify(0.012, preserve_topology=True)
    def rnd(c):
        if isinstance(c[0], (int, float)): return [round(c[0], 3), round(c[1], 3)]
        return [rnd(x) for x in c]
    m = mapping(s)
    feats.append({'n': f['properties']['nom'], 'p': f['properties']['province'], 't': m['type'], 'g': rnd(json.loads(json.dumps(m['coordinates'])))})
# contours des provinces (union des zones de santé de chaque province)
from shapely.ops import unary_union
from shapely.geometry import Polygon, MultiPolygon
provs = []
for pn in sorted({f['properties']['province'] for f in g['features']}):
    u = unary_union([shape(f['geometry']).buffer(0.002) for f in g['features'] if f['properties']['province'] == pn]).buffer(-0.002)
    parts = [x for x in (u.geoms if u.geom_type == 'MultiPolygon' else [u]) if x.area > 1e-4]
    u = MultiPolygon([Polygon(x.exterior) for x in parts]).simplify(0.012, preserve_topology=True)   # contour extérieur, sans trous parasites
    m = mapping(u)
    provs.append({'p': pn, 't': m['type'], 'g': rnd(json.loads(json.dumps(m['coordinates'])))})
data = dict(updated=last, built=dt.date.today().isoformat(), weeklyCases=wc, weeklyDeaths=wd, cum=cum, zones=zones, map=feats, prov=provs)
json.dump(data, open(sys.argv[3], 'w', encoding='utf-8'), ensure_ascii=False, separators=(',', ':'))
print('last', last, 'zones', len(zones), 'sum', sum(z['c'] for z in zones), 'nat', cum[-1], 'weeks', len(wc), file=sys.stderr)
print('top', [(z['z'], z['c'], z['c21']) for z in zones[:5]], 'wc tail', wc[-4:], 'wd tail', wd[-4:], file=sys.stderr)
