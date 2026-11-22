#!/usr/bin/env python3
"""Build a conservative XMLTV guide matching IPTV-org's US playlist.
Only genuine upstream programme entries are copied; no synthetic schedules.
"""
import csv
import gzip
import io
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

PLAYLIST = os.getenv('PLAYLIST_URL', 'https://iptv-org.github.io/iptv/countries/us.m3u')
SOURCES = [
    ('EPGShare01_US2', 'https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz'),
    ('EPGpw_US', 'https://epg.pw/xmltv/epg_US.xml.gz'),
]
OUT = Path(os.getenv('OUTPUT_DIR', 'site'))
HEADERS = {'User-Agent': 'IPTV-US-EPG/1.0 (+personal XMLTV channel audit)'}

def download(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=150) as r:
        data = r.read()
    if data[:2] == b'\x1f\x8b':
        data = gzip.decompress(data)
    return data

def attr(line, key):
    match = re.search(r'(?<!\S)' + re.escape(key) + r'="([^"]*)"', line)
    return match.group(1) if match else ''

def normalize(s):
    s = s.casefold().replace('&', ' and ')
    s = re.sub(r'\b(?:hd|fhd|uhd|4k|usa|us|tv|channel)\b', ' ', s)
    return re.sub(r'[^a-z0-9]+', '', s)

def playlist_channels(data):
    result = {}
    for line in data.decode('utf-8-sig', errors='replace').splitlines():
        if not line.startswith('#EXTINF:'):
            continue
        cid = attr(line, 'tvg-id')
        name = attr(line, 'tvg-name') or line.split(',', 1)[-1].strip()
        if not cid:
            # No stable id means TiviMate matching is unreliable; audit but don't guess.
            cid = ''
        key = cid or 'NO_ID:' + name
        result.setdefault(key, {'id': cid, 'name': name, 'logo': attr(line, 'tvg-logo')})
    return list(result.values())

def get_sources():
    loaded = []
    for label, url in SOURCES:
        try:
            root = ET.fromstring(download(url))
            if root.tag != 'tv':
                raise ValueError('Not an XMLTV <tv> document')
            ids = {}
            names = defaultdict(set)
            for c in root.findall('channel'):
                cid = c.get('id', '')
                if not cid:
                    continue
                ids[cid] = c
                for el in c.findall('display-name'):
                    if el.text and normalize(el.text):
                        names[normalize(el.text)].add(cid)
            programmes = defaultdict(list)
            for p in root.findall('programme'):
                if p.get('channel'):
                    programmes[p.get('channel')].append(p)
            loaded.append((label, ids, names, programmes))
            print(f'{label}: {len(ids)} channels, {sum(map(len, programmes.values()))} programmes')
        except Exception as exc:
            print(f'WARNING: {label} unavailable: {exc}', file=sys.stderr)
    if not loaded:
        raise RuntimeError('All guide downloads failed; refusing to publish empty EPG')
    return loaded

def main():
    channels = playlist_channels(download(PLAYLIST))
    sources = get_sources()
    root = ET.Element('tv', {'generator-info-name': 'IPTV-US-EPG (real programmes only)'})
    audit = []
    total_programmes = 0
    for ch in channels:
        pid, pname = ch['id'], ch['name']
        matches = []
        if pid:
            # Priority 1: literal channel ID; priority 2: unique normalized ID;
            # priority 3: unique normalized channel name. Never fuzzy auto-match.
            for label, ids, names, programmes in sources:
                if pid in ids and programmes.get(pid):
                    matches.append((0, label, pid, programmes[pid], 'exact_id'))
                else:
                    norm_id = normalize(pid)
                    by_id = [x for x in ids if normalize(x) == norm_id] if norm_id else []
                    if len(by_id) == 1 and programmes.get(by_id[0]):
                        matches.append((1, label, by_id[0], programmes[by_id[0]], 'normalized_id'))
                    elif normalize(pname):
                        by_name = names.get(normalize(pname), set())
                        if len(by_name) == 1:
                            found = next(iter(by_name))
                            if programmes.get(found):
                                matches.append((2, label, found, programmes[found], 'unique_name'))
        # A candidate is accepted only if its best-ranked mapping is unambiguous
        # across providers. Multiple providers with different IDs for a name are
        # reported as ambiguous rather than silently choosing one.
        matches.sort(key=lambda x: (x[0], -len(x[3])))
        chosen = None
        status = 'unmatched'
        if matches:
            best_rank = matches[0][0]
            best = [m for m in matches if m[0] == best_rank]
            if best_rank == 2 and len({m[2] for m in best}) > 1:
                status = 'ambiguous_name'
            else:
                chosen = best[0]
                status = chosen[4]
        count = 0
        if chosen:
            _, label, source_id, programmes, method = chosen
            c = ET.SubElement(root, 'channel', {'id': pid})
            ET.SubElement(c, 'display-name').text = pname
            if ch['logo']:
                ET.SubElement(c, 'icon', {'src': ch['logo']})
            seen = set()
            for p in programmes:
                signature = (p.get('start'), p.get('stop'), ''.join(p.findtext('title', default='')))
                if signature in seen:
                    continue
                seen.add(signature)
                # Preserve original programme metadata, reassign to playlist id.
                import copy
                item = copy.deepcopy(p)
                item.set('channel', pid)
                root.append(item)
                count += 1
            total_programmes += count
        audit.append({'playlist_id': pid, 'playlist_name': pname,
                      'status': status, 'source': chosen[1] if chosen else '',
                      'source_id': chosen[2] if chosen else '', 'programmes': count})
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / 'coverage.csv').open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=list(audit[0]) if audit else ['playlist_id','playlist_name','status','source','source_id','programmes'])
        writer.writeheader()
        writer.writerows(audit)
    if not total_programmes:
        raise RuntimeError('No programmes matched: refusing to publish empty EPG')
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    (OUT / 'epg.xml').write_bytes(xml)
    with gzip.open(OUT / 'epg.xml.gz', 'wb', compresslevel=6) as f:
        f.write(xml)
    matched = sum(bool(a['programmes']) for a in audit)
    summary = (f'Playlist channels: {len(channels)}\nMatched channels: {matched}\n'
               f'Unmatched channels: {len(channels)-matched}\nReal programmes: {total_programmes}\n'
               f'Sources loaded: {len(sources)}/{len(SOURCES)}\n')
    (OUT / 'summary.txt').write_text(summary, encoding='utf-8')
    (OUT / 'index.html').write_text('<!doctype html><meta charset="utf-8"><title>IPTV-US-EPG</title><h1>IPTV-US-EPG</h1><p><a href="epg.xml.gz">EPG download</a> · <a href="coverage.csv">Coverage audit</a> · <a href="summary.txt">Summary</a></p>', encoding='utf-8')
    print(summary)

if __name__ == '__main__':
    main()
