#!/usr/bin/env python3
"""IPTV-org US XMLTV matching audit and conservative EPG publisher (V2)."""
import copy
import csv
import gzip
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict, Counter
from pathlib import Path

PLAYLIST = os.getenv('PLAYLIST_URL', 'https://iptv-org.github.io/iptv/countries/us.m3u')
SOURCES = [
    ('EPGShare01_US2', 'https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz'),
    ('EPGpw_US', 'https://epg.pw/xmltv/epg_US.xml.gz'),
]
OUT = Path(os.getenv('OUTPUT_DIR', 'site'))
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; IPTV-US-EPG/2.0)'}

def download(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=180) as response:
        data = response.read()
    return gzip.decompress(data) if data.startswith(b'\x1f\x8b') else data

def attr(line, key):
    m = re.search(r'(?<!\S)' + re.escape(key) + r'="([^"]*)"', line)
    return m.group(1) if m else ''

def normalize(s):
    s = s.casefold().replace('&', ' and ')
    s = re.sub(r'\b(?:hd|fhd|uhd|4k|usa|us|tv|channel)\b', ' ', s)
    return re.sub(r'[^a-z0-9]+', '', s)

def playlist_channels(data):
    channels = []
    for line in data.decode('utf-8-sig', errors='replace').splitlines():
        if line.startswith('#EXTINF:'):
            display = line.split(',', 1)[-1].strip()
            channels.append({'id': attr(line, 'tvg-id').strip(),
                             'name': (attr(line, 'tvg-name') or display).strip(),
                             'display': display,
                             'logo': attr(line, 'tvg-logo')})
    return channels

def load_sources():
    loaded = []
    for label, url in SOURCES:
        try:
            root = ET.fromstring(download(url))
            if root.tag != 'tv':
                raise ValueError('XMLTV root is not <tv>')
            ids = {}
            names = defaultdict(set)
            normalized_ids = defaultdict(set)
            for ch in root.findall('channel'):
                cid = ch.get('id', '')
                if not cid:
                    continue
                ids[cid] = ch
                normalized_ids[normalize(cid)].add(cid)
                for el in ch.findall('display-name'):
                    if el.text and normalize(el.text):
                        names[normalize(el.text)].add(cid)
            programmes = defaultdict(list)
            for p in root.findall('programme'):
                if p.get('channel'):
                    programmes[p.get('channel')].append(p)
            loaded.append({'label': label, 'ids': ids, 'names': names,
                           'normalized_ids': normalized_ids, 'programmes': programmes})
            print(f'{label}: {len(ids)} channels, {sum(map(len, programmes.values()))} programmes', flush=True)
        except Exception as exc:
            print(f'WARNING: {label}: {exc}', file=sys.stderr, flush=True)
    return loaded

def candidates(ch, sources):
    results = []
    pid = ch['id']
    for src in sources:
        hits = []
        if pid and pid in src['ids']:
            hits.append((0, pid, 'exact_id'))
        if pid and normalize(pid):
            hits.extend((1, cid, 'normalized_id') for cid in src['normalized_ids'][normalize(pid)])
        for value in (ch['name'], ch['display']):
            if normalize(value):
                hits.extend((2, cid, 'unique_name') for cid in src['names'][normalize(value)])
        for rank, cid, method in hits:
            if src['programmes'].get(cid):
                results.append((rank, src['label'], cid, method, src['programmes'][cid]))
    unique = {}
    for hit in results:
        key = (hit[1], hit[2])
        if key not in unique or hit[0] < unique[key][0]:
            unique[key] = hit
    return sorted(unique.values(), key=lambda x: (x[0], -len(x[4]), x[1], x[2]))

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    channels = playlist_channels(download(PLAYLIST))
    sources = load_sources()
    print(f'Playlist entries: {len(channels)}; entries without tvg-id: {sum(not c["id"] for c in channels)}', flush=True)
    root = ET.Element('tv', {'generator-info-name': 'IPTV-US-EPG V2; real programmes only'})
    audit = []
    emitted_ids = set()
    matched_ids = set()
    programme_signatures = set()
    total = 0
    for ch in channels:
        pid = ch['id']
        hits = candidates(ch, sources)
        best = [x for x in hits if x[0] == hits[0][0]] if hits else []
        # Two providers may describe the same channel under different IDs.
        # For name-only matches, require a single unique source ID across providers.
        ambiguous = len({x[2] for x in best}) > 1 and best[0][0] == 2 if best else False
        chosen = best[0] if best and not ambiguous else None
        status = ('missing_playlist_id' if not pid else 'unmatched')
        if ambiguous:
            status = 'ambiguous_name'
        elif chosen and not pid:
            status = 'candidate_missing_playlist_id'
        elif chosen:
            status = chosen[3]
        count = 0
        if chosen and pid and pid not in emitted_ids:
            emitted_ids.add(pid)
            matched_ids.add(pid)
            c = ET.SubElement(root, 'channel', {'id': pid})
            ET.SubElement(c, 'display-name').text = ch['name']
            if ch['logo']:
                ET.SubElement(c, 'icon', {'src': ch['logo']})
            for p in chosen[4]:
                signature = (pid, p.get('start'), p.get('stop'), p.findtext('title', default=''))
                if signature in programme_signatures:
                    continue
                programme_signatures.add(signature)
                item = copy.deepcopy(p)
                item.set('channel', pid)
                root.append(item)
                count += 1
            total += count
        audit.append({'playlist_id': pid, 'playlist_name': ch['name'], 'status': status,
                      'source': chosen[1] if chosen else '',
                      'source_id': chosen[2] if chosen else '',
                      'programmes': count,
                      'candidate_count': len(hits)})
    fields = ['playlist_id', 'playlist_name', 'status', 'source', 'source_id', 'programmes', 'candidate_count']
    with (OUT / 'coverage.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(audit)
    counts = Counter(row['status'] for row in audit)
    summary = (f'Playlist entries: {len(channels)}\n'
               f'Entries without tvg-id: {sum(not c["id"] for c in channels)}\n'
               f'Unique matched playlist IDs: {len(matched_ids)}\n'
               f'Real programmes: {total}\n'
               f'Sources loaded: {len(sources)}/{len(SOURCES)}\n\n'
               + 'Match status counts:\n'
               + ''.join(f'  {key}: {value}\n' for key, value in sorted(counts.items())))
    (OUT / 'summary.txt').write_text(summary, encoding='utf-8')
    (OUT / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>IPTV-US-EPG</title><h1>IPTV-US-EPG</h1><p><a href="epg.xml.gz">Guide</a> | <a href="coverage.csv">Coverage CSV</a> | <a href="summary.txt">Summary</a></p>', encoding='utf-8')
    print(summary, flush=True)
    if not total:
        print('WARNING: No matched programmes. Audit files were generated; empty EPG will not be published.', file=sys.stderr)
        raise RuntimeError('No programmes matched; see summary.txt and coverage.csv in workflow artifacts')
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    (OUT / 'epg.xml').write_bytes(xml)
    with gzip.open(OUT / 'epg.xml.gz', 'wb', compresslevel=6) as f:
        f.write(xml)

if __name__ == '__main__':
    main()
