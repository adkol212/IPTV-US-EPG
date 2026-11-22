#!/usr/bin/env python3
"""IPTV-org US XMLTV conservative channel matcher (V4)."""
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
HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; IPTV-US-EPG/4.0)'}

def download(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=180) as response:
        data = response.read()
    return gzip.decompress(data) if data.startswith(b'\x1f\x8b') else data

def attr(line, key):
    m = re.search(r'(?<!\S)' + re.escape(key) + r'="([^"]*)"', line)
    return m.group(1) if m else ''

def normalize(s):
    """Conservative channel-name key; remove technical labels."""
    s = (s or '').casefold().replace('&', ' and ')
    s = re.sub(r'\((?:\d{3,4}p|4k|uhd|fhd|hd|sd|united states|usa)\)', ' ', s)
    s = re.sub(r'\[(?:geo-blocked|not 24/7|offline|\d{3,4}p)\]', ' ', s)
    s = re.sub(r'\b(?:uhd|fhd|hd|sd|4k)\b', ' ', s)
    return re.sub(r'[^a-z0-9]+', '', s)

def id_keys(raw):
    """Use both the full ID and its stream/region-stripped variant."""
    raw = (raw or '').split('@', 1)[0]
    keys = {normalize(raw)}
    raw = re.sub(r'\.us2?$', '', raw, flags=re.I)
    keys.add(normalize(raw))
    return {k for k in keys if k}

def name_keys(ch):
    return {normalize(x) for x in (ch['name'], ch['display']) if normalize(x)}

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
                for key in id_keys(cid):
                    normalized_ids[key].add(cid)
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
    """Return high-confidence matches only; avoid ambiguous source channel choices."""
    results = []
    pid = ch['id']
    for src in sources:
        hits = []
        if pid and pid in src['ids']:
            hits.append((0, pid, 'exact_id'))
        for key in id_keys(pid):
            hits.extend((1, cid, 'normalized_id') for cid in src['normalized_ids'].get(key, ()))
        for key in name_keys(ch):
            hits.extend((2, cid, 'normalized_name') for cid in src['names'].get(key, ()))
        for rank, cid, method in hits:
            if src['programmes'].get(cid):
                results.append((rank, src['label'], cid, method, src['programmes'][cid]))
    unique = {}
    for hit in results:
        key = (hit[1], hit[2])
        if key not in unique or hit[0] < unique[key][0]:
            unique[key] = hit
    return sorted(unique.values(), key=lambda x: (x[0], SOURCES.index(next(s for s in SOURCES if s[0] == x[1])), x[2]))

def write_id_audit(channels, sources):
    """Write raw IDs and display names before attempting any matching."""
    rows = []
    for ch in channels[:15]:
        rows.append({'source': 'PLAYLIST', 'channel_id': ch['id'],
                     'display_name': ch['display'], 'tvg_name': ch['name']})
    for src in sources:
        for cid, node in list(src['ids'].items())[:15]:
            rows.append({'source': src['label'], 'channel_id': cid,
                         'display_name': ' | '.join((el.text or '').strip()
                             for el in node.findall('display-name') if el.text)[:250],
                         'tvg_name': ''})
    with (OUT / 'id_samples.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['source', 'channel_id', 'display_name', 'tvg_name'])
        writer.writeheader()
        writer.writerows(rows)
    print('\n=== CHANNEL ID AUDIT (first 15 from each source) ===', flush=True)
    for row in rows:
        print(f"{row['source']}: ID={row['channel_id']!r}; name={row['display_name']!r}", flush=True)
    print('=== END ID AUDIT ===\n', flush=True)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    channels = playlist_channels(download(PLAYLIST))
    sources = load_sources()
    write_id_audit(channels, sources)
    print(f'Playlist entries: {len(channels)}; entries without tvg-id: {sum(not c["id"] for c in channels)}', flush=True)
    root = ET.Element('tv', {'generator-info-name': 'IPTV-US-EPG V4; real programmes only'})
    audit = []
    candidates_audit = []
    emitted_ids = set()
    matched_ids = set()
    programme_signatures = set()
    total = 0
    for ch in channels:
        pid = ch['id']
        hits = candidates(ch, sources)
        best = [x for x in hits if x[0] == hits[0][0]] if hits else []
        for hit in hits[:10]:
            candidates_audit.append({'playlist_id': pid, 'playlist_name': ch['name'],
                                     'rank': hit[0], 'provider': hit[1],
                                     'provider_id': hit[2], 'method': hit[3]})
        # Distinct channel IDs within the SAME source at the best rank are ambiguous.
        # A single channel in each of two providers is not itself ambiguous.
        best_by_source = defaultdict(set)
        for hit in best:
            best_by_source[hit[1]].add(hit[2])
        unambiguous_sources = {label for label, ids in best_by_source.items() if len(ids) == 1}
        ambiguous = bool(best) and not unambiguous_sources
        chosen = next((hit for hit in best if hit[1] in unambiguous_sources), None)
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
    with (OUT / 'candidates.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['playlist_id','playlist_name','rank','provider','provider_id','method'])
        writer.writeheader()
        writer.writerows(candidates_audit)
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
        print('WARNING: No matched programmes. Diagnostics will be uploaded; no empty EPG will be published.', file=sys.stderr, flush=True)
        raise RuntimeError('No programmes matched; download the iptv-epg-diagnostics artifact (id_samples.csv, coverage.csv, summary.txt)')
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    (OUT / 'epg.xml').write_bytes(xml)
    with gzip.open(OUT / 'epg.xml.gz', 'wb', compresslevel=6) as f:
        f.write(xml)

if __name__ == '__main__':
    main()
