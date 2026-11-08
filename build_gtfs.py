#!/usr/bin/env python3
"""Extract a compact, date-valid schedule for Wierna 01 from Warsaw GTFS.
Python 3.11+, stdlib only. Intended for GitHub Actions; does not publish secrets.
"""
import csv
import io
import json
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, date, timedelta, time
from pathlib import Path
from zoneinfo import ZoneInfo

FEED_URL = os.environ.get('GTFS_FEED_URL', 'https://mkuran.pl/gtfs/warsaw.zip')
OUTPUT = Path(os.environ.get('GTFS_OUTPUT', 'data/wierna01.json'))
DAYS = 10
TZ = ZoneInfo('Europe/Warsaw')


def gtfs_rows(z, name):
    with z.open(name) as stream, io.TextIOWrapper(stream, encoding='utf-8-sig', newline='') as txt:
        for row in csv.DictReader(txt):
            yield row


def compact(value):
    return re.sub(r'[^0-9a-z]+', '', str(value or '').lower())


def find_stop_ids(z):
    matches = []
    for row in gtfs_rows(z, 'stops.txt'):
        stop_id = row.get('stop_id', '')
        label = ' '.join((row.get('stop_name', ''), row.get('town_name', '')))
        # Prefer the platform code and stop area; many Polish GTFS feeds encode both in stop_id.
        code = compact(row.get('stop_code', ''))
        platform = compact(row.get('platform_code', ''))
        sid = compact(stop_id)
        named = 'wierna' in compact(label)
        is_target = (code == '1506' and platform in ('01', '1')) or (named and (platform in ('01', '1') or sid.endswith('150601') or sid.endswith('15061')))
        if is_target:
            matches.append((stop_id, row))
    if not matches:
        raise RuntimeError('Nie znaleziono Wierna 01 (1506/01) w stops.txt; nie publikuję danych.')
    # Reject obvious false positives, but allow parent and child stop_id matches for same platform.
    print('Found stop IDs:', ', '.join(x[0] for x in matches))
    return {x[0] for x in matches}


def calendar_active(z, start, end):
    active = defaultdict(set)
    base = {}
    if 'calendar.txt' in z.namelist():
        for r in gtfs_rows(z, 'calendar.txt'):
            base[r['service_id']] = r
    exceptions = defaultdict(dict)
    if 'calendar_dates.txt' in z.namelist():
        for r in gtfs_rows(z, 'calendar_dates.txt'):
            d = r.get('date', '')
            if start.strftime('%Y%m%d') <= d <= end.strftime('%Y%m%d'):
                exceptions[d][r['service_id']] = r.get('exception_type', '')
    day = start
    while day <= end:
        stamp = day.strftime('%Y%m%d')
        dow = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')[day.weekday()]
        for sid, r in base.items():
            if r.get('start_date', '99999999') <= stamp <= r.get('end_date', '00000000') and r.get(dow) == '1':
                active[stamp].add(sid)
        for sid, mode in exceptions[stamp].items():
            if mode == '1':
                active[stamp].add(sid)
            elif mode == '2':
                active[stamp].discard(sid)
        day += timedelta(days=1)
    if not any(active.values()):
        raise RuntimeError('GTFS nie ma aktywnych kursów w wybranych dniach')
    return active


def main():
    now = datetime.now(TZ)
    start_day = now.date() - timedelta(days=1)
    end_day = now.date() + timedelta(days=DAYS)
    # Download in a streamed manner: the full feed is ~90 MiB; Apps Script cannot unzip it reliably.
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / 'warsaw.zip'
        req = urllib.request.Request(FEED_URL, headers={'User-Agent': 'WallPanelGTFS/1.0 (personal transit dashboard)'})
        with urllib.request.urlopen(req, timeout=180) as src, open(archive, 'wb') as dst:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dst.write(block)
        print('Downloaded', archive.stat().st_size, 'bytes')
        with zipfile.ZipFile(archive) as z:
            required = ('stops.txt', 'stop_times.txt', 'trips.txt', 'routes.txt')
            for name in required:
                if name not in z.namelist():
                    raise RuntimeError('Brak '+name+' w GTFS')
            stop_ids = find_stop_ids(z)
            services = calendar_active(z, start_day, end_day)
            # First isolate departures from one stop, then resolve their trip/service/route.
            departures_by_trip = defaultdict(list)
            for r in gtfs_rows(z, 'stop_times.txt'):
                if r.get('stop_id') not in stop_ids:
                    continue
                dep = r.get('departure_time') or r.get('arrival_time') or ''
                if not re.match(r'^\d{1,3}:\d{2}:\d{2}$', dep):
                    continue
                h, m, s = map(int, dep.split(':'))
                if m >= 60 or s >= 60 or h > 47:
                    continue
                departures_by_trip[r['trip_id']].append((h * 3600 + m * 60 + s, r.get('stop_id')))
            if not departures_by_trip:
                raise RuntimeError('Brak kursów dla Wierna 01 w stop_times.txt')
            trips = {}
            for r in gtfs_rows(z, 'trips.txt'):
                trip_id = r.get('trip_id')
                if trip_id in departures_by_trip:
                    trips[trip_id] = (r.get('service_id', ''), r.get('route_id', ''), r.get('trip_headsign', '') or '')
            routes = {}
            for r in gtfs_rows(z, 'routes.txt'):
                routes[r['route_id']] = r.get('route_short_name') or r.get('route_long_name') or '?'
            calendar = defaultdict(list)
            for stamp, active_ids in services.items():
                service_date = datetime.strptime(stamp, '%Y%m%d').date()
                for trip_id, values in trips.items():
                    service, route_id, direction = values
                    if service not in active_ids:
                        continue
                    line = routes.get(route_id, '?')
                    for seconds, stopid in departures_by_trip[trip_id]:
                        target = service_date + timedelta(days=seconds // 86400)
                        if target < now.date() or target > end_day:
                            continue
                        sec = seconds % 86400
                        local = datetime.combine(target, time(sec // 3600, (sec % 3600) // 60, sec % 60), TZ)
                        calendar[target.isoformat()].append({'line': str(line)[:14], 'direction': str(direction)[:95], 'time': local.strftime('%H:%M'), 'departure': local.isoformat(timespec='seconds')})
            for key in calendar:
                calendar[key].sort(key=lambda x: x['departure'])
            today = now.date().isoformat()
            if not calendar.get(today):
                raise RuntimeError('Brak odjazdów Wierna 01 na dziś; pliku nie publikuję bez weryfikacji')
            OUTPUT.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'schema': 1, 'stop': 'Wierna 01', 'area': '1506', 'platform': '01',
                'timezone': 'Europe/Warsaw', 'source': 'GTFS Warszawa (MKuranowski / ZTM)',
                'feedUrl': FEED_URL, 'generatedAt': now.isoformat(timespec='seconds'),
                'validFrom': today, 'validTo': end_day.isoformat(),
                'days': dict(sorted(calendar.items())),
                'attribution': 'ZTM Warszawa; Mikołaj Kuranowski; OpenStreetMap contributors — szczegóły: https://mkuran.pl/gtfs/'
            }
            temp = OUTPUT.with_suffix('.tmp')
            temp.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
            temp.replace(OUTPUT)
            print('Wrote', OUTPUT, 'today', len(calendar[today]), 'departures,', OUTPUT.stat().st_size, 'bytes')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR:', str(exc), file=sys.stderr)
        sys.exit(1)
