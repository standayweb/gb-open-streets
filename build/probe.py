"""Find current download URLs and print file layouts. Diagnostic only."""
import json, sys, urllib.request, zipfile, io, os, subprocess

def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'gb-open-streets'}), timeout=120) as r:
        return r.read()

for prod in ('OpenRoads', 'OpenNames', 'OpenUPRN', 'CodePointOpen'):
    try:
        d = json.loads(get('https://api.os.uk/downloads/v1/products/%s/downloads' % prod))
        print('OS', prod, json.dumps([{k: x.get(k) for k in ('area', 'format', 'size', 'fileName', 'url')} for x in d])[:1500])
    except Exception as e:
        print('OS', prod, 'ERROR', e)

for q in ('ONS UPRN Directory', 'ONSUD', 'Wards names and codes', 'Local Authority Districts names and codes'):
    try:
        u = ('https://www.arcgis.com/sharing/rest/search?f=json&num=12&sortField=modified&sortOrder=desc&q='
             + urllib.parse.quote('"%s" owner:ONSGeography_data' % q))
        d = json.loads(get(u))
        for r in d.get('results', []):
            print('AGOL', q, '|', r['id'], '|', r['type'], '|', r['title'], '|', r.get('size'), '|', r.get('modified'))
    except Exception as e:
        print('AGOL', q, 'ERROR', e)
