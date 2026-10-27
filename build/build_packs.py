#!/usr/bin/env python3
"""Build one data pack per local authority district (council) in Great Britain.

Sources (all open data, Open Government Licence v3):
  ONS UPRN Directory (ONSUD)  address points: position, postcode, ward, council
  OS Open Roads               road centre lines with names and road numbers
  OS Open Names               populated places (cities, towns, villages, hamlets)

A pack is gzipped JSON:
  v, lad, name, nation, built, sources
  wards   [[code, name], ...]
  pcs     ["AB1 2CD", ...]
  addr    {o: [lat0, lon0], d: [[dlat, dlon, pc_index, ward_index], ...]}
          positions in 1e-5 degree units relative to o
  roads   {o: [lat0, lon0], d: [[name, class, [[dlat, dlon], ...]], ...]}
  places  [[name, local_type, lat, lon], ...]

No personal data: an address point is a position and a postcode, nothing more.

  python3 build_packs.py --workdir dl --out packs [--only S12000006,E07000121]
"""
import argparse, csv, glob, gzip, io, json, math, os, re, shutil, sqlite3, struct
import subprocess, sys, time, urllib.parse, urllib.request, zipfile
from collections import defaultdict

import numpy as np
from pyproj import Transformer

OS_API = 'https://api.os.uk/downloads/v1/products/%s/downloads'
AGOL = 'https://www.arcgis.com/sharing/rest'
UA = {'User-Agent': 'gb-open-streets'}
CELL = 1000          # metres: occupancy grid for assigning roads/places to councils
ROAD_PAD = 1         # cells of padding round a council's addresses for roads
PLACE_PAD = 3        # cells of padding for place names
NATIONS = {'E92000001': 'England', 'S92000003': 'Scotland', 'W92000004': 'Wales'}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def get_json(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return json.loads(r.read())


def download(url, path):
    if os.path.exists(path):
        log('have', path)
        return path
    subprocess.run(['curl', '-sSfL', '--retry', '5', '--retry-delay', '10', '-o', path + '.part', url],
                   check=True)
    os.rename(path + '.part', path)
    log('downloaded', path, os.path.getsize(path) // 1_000_000, 'MB')
    return path


def os_url(product, fmt):
    for x in get_json(OS_API % product):
        if x.get('area') == 'GB' and x.get('format') == fmt:
            return x['url'], x.get('fileName', '')
    raise SystemExit('OS %s has no GB %s download' % (product, fmt))


def onsud_item():
    q = urllib.parse.quote('"ONS UPRN Directory" owner:ONSGeography_data')
    res = get_json(AGOL + '/search?f=json&num=40&sortField=modified&sortOrder=desc&q=' + q)['results']
    for r in res:
        t = r['title']
        if t.startswith('ONS UPRN Directory (') and 'Guide' not in t and r['type'] == 'CSV Collection':
            return r['id'], t
    raise SystemExit('could not find the current ONS UPRN Directory')


def col(header, pattern):
    for i, h in enumerate(header):
        if re.fullmatch(pattern, h.strip().lower()):
            return i
    raise SystemExit('column %s not found in %s' % (pattern, header[:12]))


# ---------------------------------------------------------------- ONSUD
def read_onsud(zpath, tmp, only):
    """Stream every address into one temp file per council. Returns names and occupancy cells."""
    z = zipfile.ZipFile(zpath)
    docs = {i.filename: i for i in z.infolist()}

    def names(prefix_re):
        f = [n for n in docs if re.search(prefix_re, n.split('/')[-1]) and n.lower().endswith('.csv')]
        if not f:
            raise SystemExit('names file %s missing' % prefix_re)
        out = {}
        with z.open(sorted(f)[-1]) as fh:
            rd = csv.reader(io.TextIOWrapper(fh, 'utf-8-sig'))
            h = next(rd)
            ci, ni = 0, 1
            for row in rd:
                if row and row[ci]:
                    out[row[ci]] = row[ni]
        return out

    lad_names = names(r'^LAD .*names and codes')
    ward_names = names(r'^WD .*names and codes')
    log('names:', len(lad_names), 'councils,', len(ward_names), 'wards')

    files, cells, counts, nation, wards_by_lad = {}, defaultdict(set), defaultdict(int), {}, defaultdict(set)
    data = [n for n in docs if n.startswith('Data/') and n.lower().endswith('.csv')]
    for name in sorted(data):
        with z.open(name) as fh:
            rd = csv.reader(io.TextIOWrapper(fh, 'utf-8-sig'))
            h = next(rd)
            ie, inn, ipc = col(h, 'gridgb1e'), col(h, 'gridgb1n'), col(h, 'pcds')
            ilad, iwd, ictry = col(h, r'lad\d\dcd'), col(h, r'wd\d\dcd'), col(h, r'ctry\d\dcd')
            n = 0
            for row in rd:
                lad, ctry, pc = row[ilad], row[ictry], row[ipc]
                if ctry not in NATIONS or not pc or not lad or lad.endswith('9999999'):
                    continue
                if only and lad not in only:
                    continue
                e, nn = float(row[ie]), float(row[inn])
                f = files.get(lad)
                if f is None:
                    f = files[lad] = open(os.path.join(tmp, lad + '.addr'), 'w')
                    nation[lad] = NATIONS[ctry]
                f.write('%d,%d,%s,%s\n' % (e, nn, pc, row[iwd]))
                cells[lad].add((int(e) // CELL, int(nn) // CELL))
                wards_by_lad[lad].add(row[iwd])
                counts[lad] += 1
                n += 1
        log(name.split('/')[-1], n, 'addresses kept')
    for f in files.values():
        f.close()
    return lad_names, ward_names, cells, counts, nation, wards_by_lad


def padded(cellset, pad):
    out = set()
    for cx, cy in cellset:
        for dx in range(-pad, pad + 1):
            for dy in range(-pad, pad + 1):
                out.add((cx + dx, cy + dy))
    return out


def cell_index(cells, pad):
    idx = defaultdict(list)
    for lad, cs in cells.items():
        for c in padded(cs, pad):
            idx[c].append(lad)
    return idx


# ---------------------------------------------------------------- roads
def gpkg_line(blob):
    """GeoPackage geometry blob -> numpy (n, 2) coordinates. Handles (Multi)LineString, 2D or Z."""
    flags = blob[3]
    env = (0, 32, 48, 48, 64)[(flags >> 1) & 7]
    return wkb_lines(blob, 8 + env)


def wkb_type(t):
    """(base type, dimensions) for ISO or EWKB type codes."""
    if t & 0xC0000000:
        return t & 0xFFFF, 2 + bool(t & 0x80000000) + bool(t & 0x40000000)
    return t % 1000, {0: 2, 1: 3, 2: 3, 3: 4}[t // 1000]


def wkb_lines(b, off):
    bo = '<' if b[off] == 1 else '>'
    base, dims = wkb_type(struct.unpack_from(bo + 'I', b, off + 1)[0])
    if base == 2:
        n = struct.unpack_from(bo + 'I', b, off + 5)[0]
        return [np.frombuffer(b, dtype=bo + 'f8', count=n * dims, offset=off + 9).reshape(n, dims)[:, :2]]
    if base == 5:
        n = struct.unpack_from(bo + 'I', b, off + 5)[0]
        out, p = [], off + 9
        for _ in range(n):
            bo2 = '<' if b[p] == 1 else '>'
            _, d2 = wkb_type(struct.unpack_from(bo2 + 'I', b, p + 1)[0])
            m = struct.unpack_from(bo2 + 'I', b, p + 5)[0]
            out.append(np.frombuffer(b, dtype=bo2 + 'f8', count=m * d2, offset=p + 9).reshape(m, d2)[:, :2])
            p += 9 + m * d2 * 8
        return out
    return []


def read_roads(gpkg, tmp, idx):
    db = sqlite3.connect(gpkg)
    files, kept, seen = {}, 0, 0
    q = ('select name_1, road_classification_number, road_classification, geometry '
         'from road_link where road_classification != "Motorway"')
    for name, number, cls, geom in db.execute(q):
        seen += 1
        label = name or number
        if not label or not geom:
            continue
        for line in gpkg_line(geom):
            lads = set()
            for x, y in line[:: max(1, len(line) // 8)]:
                lads.update(idx.get((int(x) // CELL, int(y) // CELL), ()))
            x, y = line[-1]
            lads.update(idx.get((int(x) // CELL, int(y) // CELL), ()))
            if not lads:
                continue
            rec = json.dumps([label, cls[0] if cls else '', np.round(line).astype(int).tolist()],
                             separators=(',', ':'))
            for lad in lads:
                f = files.get(lad)
                if f is None:
                    f = files[lad] = open(os.path.join(tmp, lad + '.roads'), 'w')
                f.write(rec + '\n')
            kept += 1
        if seen % 500000 == 0:
            log('roads read', seen, 'kept', kept)
    for f in files.values():
        f.close()
    log('roads done', seen, 'read,', kept, 'kept')


# ---------------------------------------------------------------- places
def read_places(zpath, idx):
    z = zipfile.ZipFile(zpath)
    places = defaultdict(list)
    keep = {'City', 'Town', 'Village', 'Hamlet', 'Other Settlement', 'Suburban Area'}
    for n in z.namelist():
        if not (n.startswith('Data/') and n.endswith('.csv')):
            continue
        with z.open(n) as fh:
            for row in csv.reader(io.TextIOWrapper(fh, 'utf-8-sig')):
                if len(row) < 10 or row[6] != 'populatedPlace' or row[7] not in keep:
                    continue
                x, y = float(row[8]), float(row[9])
                for lad in idx.get((int(x) // CELL, int(y) // CELL), ()):
                    places[lad].append((row[2], row[7], x, y))
    log('places for', len(places), 'councils')
    return places


# ---------------------------------------------------------------- packs
TO_WGS = Transformer.from_crs(27700, 4326, always_xy=True)


def wgs(xy):
    lon, lat = TO_WGS.transform(xy[:, 0], xy[:, 1])
    return np.round(lat * 1e5).astype(np.int64), np.round(lon * 1e5).astype(np.int64)


def build_pack(lad, name, nation, ward_names, tmp, places, out, sources, built):
    rows = [l.rstrip('\n').split(',') for l in open(os.path.join(tmp, lad + '.addr'))]
    xy = np.array([[float(r[0]), float(r[1])] for r in rows])
    la, lo = wgs(xy)
    o = [int(la.min()), int(lo.min())]
    pcs, pci, wards, wdi, d = [], {}, [], {}, []
    for r, a, b in zip(rows, la, lo):
        if r[2] not in pci:
            pci[r[2]] = len(pcs); pcs.append(r[2])
        if r[3] not in wdi:
            wdi[r[3]] = len(wards); wards.append([r[3], ward_names.get(r[3], r[3])])
        d.append([int(a) - o[0], int(b) - o[1], pci[r[2]], wdi[r[3]]])
    roads = []
    rp = os.path.join(tmp, lad + '.roads')
    if os.path.exists(rp):
        for l in open(rp):
            label, cls, pts = json.loads(l)
            p = np.array(pts, dtype=float)
            a, b = wgs(p)
            roads.append([label, cls, [[int(u) - o[0], int(v) - o[1]] for u, v in zip(a, b)]])
    pl = []
    for nm, typ, x, y in places.get(lad, []):
        a, b = wgs(np.array([[x, y]]))
        pl.append([nm, typ, int(a[0]) / 1e5, int(b[0]) / 1e5])
    pack = {'v': 1, 'lad': lad, 'name': name, 'nation': nation, 'built': built, 'sources': sources,
            'wards': wards, 'pcs': pcs, 'addr': {'o': [o[0] / 1e5, o[1] / 1e5], 'd': d},
            'roads': {'o': [o[0] / 1e5, o[1] / 1e5], 'd': roads}, 'places': pl}
    path = os.path.join(out, lad + '.json.gz')
    with gzip.open(path, 'wt', compresslevel=9) as f:
        json.dump(pack, f, separators=(',', ':'), ensure_ascii=False)
    return {'code': lad, 'name': name, 'nation': nation, 'addresses': len(d), 'postcodes': len(pcs),
            'roads': len(roads), 'file': lad + '.json.gz', 'bytes': os.path.getsize(path)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default='dl')
    ap.add_argument('--out', default='packs')
    ap.add_argument('--only', default='')
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(',') if x.strip()}
    os.makedirs(a.workdir, exist_ok=True)
    tmp = os.path.join(a.workdir, 'tmp')
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    os.makedirs(a.out, exist_ok=True)
    built = time.strftime('%Y-%m-%d')

    item, onsud_title = onsud_item()
    roads_url, roads_file = os_url('OpenRoads', 'GeoPackage')
    names_url, names_file = os_url('OpenNames', 'CSV')
    sources = {
        'addresses': onsud_title + ', Office for National Statistics',
        'roads': 'OS Open Roads (%s), Ordnance Survey' % roads_file,
        'places': 'OS Open Names (%s), Ordnance Survey' % names_file,
        'licence': 'Contains OS data (c) Crown copyright and database right %s. Contains National '
                   'Statistics data (c) Crown copyright and database right %s. Open Government '
                   'Licence v3.0.' % (built[:4], built[:4]),
    }
    log('sources', json.dumps(sources))

    z = download(AGOL + '/content/items/%s/data' % item, os.path.join(a.workdir, 'onsud.zip'))
    lad_names, ward_names, cells, counts, nation, _ = read_onsud(z, tmp, only)
    os.remove(z)
    road_idx = cell_index(cells, ROAD_PAD)
    place_idx = cell_index(cells, PLACE_PAD)

    z = download(roads_url, os.path.join(a.workdir, 'roads.zip'))
    with zipfile.ZipFile(z) as zz:
        g = [n for n in zz.namelist() if n.endswith('.gpkg')][0]
        zz.extract(g, a.workdir)
    os.remove(z)
    read_roads(os.path.join(a.workdir, g), tmp, road_idx)
    os.remove(os.path.join(a.workdir, g))

    z = download(names_url, os.path.join(a.workdir, 'names.zip'))
    places = read_places(z, place_idx)
    os.remove(z)

    index = []
    for lad in sorted(counts):
        info = build_pack(lad, lad_names.get(lad, lad), nation[lad], ward_names, tmp, places,
                          a.out, sources, built)
        index.append(info)
        log('pack', lad, info['name'], info['addresses'], 'addresses', info['roads'], 'roads',
            info['bytes'] // 1000, 'kB')
    with open(os.path.join(a.out, 'index.json'), 'w') as f:
        json.dump({'built': built, 'sources': sources, 'councils': index}, f, indent=1,
                  ensure_ascii=False)
    log('done:', len(index), 'packs')


if __name__ == '__main__':
    main()
