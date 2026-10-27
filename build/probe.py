"""Download sources and print their internal layout. Diagnostic only."""
import json, sys, os, urllib.request, urllib.parse, zipfile, sqlite3, subprocess, io, csv

def dl(url, path):
    subprocess.run(['curl', '-sSL', '--retry', '3', '-o', path, url], check=True)
    print('downloaded', path, os.path.getsize(path), flush=True)

os.makedirs('dl', exist_ok=True)
# ONSUD
dl('https://www.arcgis.com/sharing/rest/content/items/4198f5e0d6654d8a9661d0c15d4f912b/data', 'dl/onsud.zip')
z = zipfile.ZipFile('dl/onsud.zip')
for i in z.infolist(): print('ONSUD', i.filename, i.file_size)
for i in z.infolist():
    if i.filename.lower().endswith('.csv'):
        with z.open(i) as f:
            head = [next(f).decode('utf-8-sig', 'replace').strip() for _ in range(3)]
        print('ONSUD HEAD', i.filename, head)
os.remove('dl/onsud.zip')
# Open Names
dl('https://api.os.uk/downloads/v1/products/OpenNames/downloads?area=GB&format=CSV&redirect', 'dl/names.zip')
z = zipfile.ZipFile('dl/names.zip')
names = [i.filename for i in z.infolist()]
print('NAMES files', len(names), names[:8], [n for n in names if 'oc' in n.lower() or 'header' in n.lower()][:5])
for n in names:
    if 'header' in n.lower(): print('NAMES HEADER', z.read(n)[:2000])
data = [n for n in names if n.endswith('.csv') and 'header' not in n.lower()]
with z.open(data[0]) as f:
    for _ in range(3): print('NAMES ROW', f.readline()[:600])
os.remove('dl/names.zip')
# Open Roads
dl('https://api.os.uk/downloads/v1/products/OpenRoads/downloads?area=GB&format=GeoPackage&redirect', 'dl/roads.zip')
z = zipfile.ZipFile('dl/roads.zip')
for i in z.infolist(): print('ROADS', i.filename, i.file_size)
g = [i.filename for i in z.infolist() if i.filename.endswith('.gpkg')][0]
z.extract(g, 'dl'); os.remove('dl/roads.zip')
db = sqlite3.connect(os.path.join('dl', g))
for (t,) in db.execute("select table_name from gpkg_contents"):
    cols = [r[1] for r in db.execute('pragma table_info("%s")' % t)]
    n = db.execute('select count(*) from "%s"' % t).fetchone()[0]
    print('ROADS TABLE', t, n, cols)
print('ROADS SRS', db.execute('select table_name, srs_id from gpkg_geometry_columns').fetchall())
row = db.execute('select * from road_link limit 1').fetchone()
print('ROADS SAMPLE', [ (x[:60] if isinstance(x,(bytes,str)) else x) for x in row])
print('ROADS CLASSES', db.execute('select road_classification, count(*) from road_link group by 1').fetchall())
