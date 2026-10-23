"""Recorded multi-date agreement is evidence, never a pagination-total override."""
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_EVEN
import json
from pathlib import Path

import httpx
import pytest

from outage_explorer import extract_eia as extraction, refresh_eia as refresh

EVIDENCE = Path(__file__).resolve().parents[3] / 'docs/evidence/eia-pagination-2026-10-18_19.json'
KEY = 'offline-evidence-test-key'
START, END = date(2026, 10, 18), date(2026, 10, 19)


@pytest.fixture
def recording():
    return json.loads(EVIDENCE.read_text())


def first(recording, route):
    return next(r for r in recording['requests'] if r['route'] == route and r['suffix'] == '/data/'
                and r['params'].get('offset') == 0 and 'facets[facility][]' not in r['params'])['response']


def test_recorded_keys_measures_and_facets_support_but_do_not_prove_count_hypothesis(recording):
    assert len(recording['requests']) == recording['request_budget'] == 9
    assert all(r['http_status'] == 200 and not r['warning'] for r in recording['requests'])
    facility = first(recording, 'facility-nuclear-outages')
    generator = first(recording, 'generator-nuclear-outages')
    national = first(recording, 'us-nuclear-outages')
    assert (facility['total'], len(facility['data'])) == ('190', 110)
    assert (generator['total'], len(generator['data'])) == ('190', 190)
    assert (national['total'], len(national['data'])) == ('2', 2)
    grouped = defaultdict(list)
    for route in extraction.ROUTES:
        rows = first(recording, route)['data']
        keys = [extraction.validate_record(row, route, START, END)[1] for row in rows]
        assert len(keys) == len(set(keys))
    for row in generator['data']:
        grouped[row['period'], row['facility']].append(row)
    assert set(grouped) == {(row['period'], row['facility']) for row in facility['data']}
    for row in facility['data']:
        for measure in ('capacity', 'outage'):
            assert Decimal(row[measure]) == sum(Decimal(g[measure]) for g in grouped[row['period'], row['facility']])
        assert Decimal(row['percentOutage']) == (Decimal(row['outage']) / Decimal(row['capacity']) * 100).quantize(
            Decimal('.01'), rounding=ROUND_HALF_EVEN)
    for row in national['data']:
        for measure in ('capacity', 'outage'):
            assert Decimal(row[measure]) == sum(Decimal(f[measure]) for f in facility['data'] if f['period'] == row['period'])
    probes = [r for r in recording['requests'] if 'facets[facility][]' in r['params']]
    assert [(r['params']['facets[facility][]'], r['response']['total'], len(r['response']['data'])) for r in probes] == [
        ('1715', '2', 2), ('3265', '6', 2)]
    # No missing rows are synthesized and no expected total is rewritten.
    assert len(facility['data']) != int(facility['total'])


@pytest.mark.parametrize('mode', ['window', 'incremental'])
def test_recorded_terminal_count_exception_publishes_validated_observations(recording, tmp_path, mode):
    old = json.loads((Path(__file__).parent / 'fixtures/eia_nuclear_sample.json').read_text())
    requested_offsets = defaultdict(list)
    def handler(request):
        route = request.url.path.removeprefix('/v2/nuclear-outages/').split('/')[0]
        if not request.url.path.endswith('/data/'):
            # Earlier recorded metadata for national/generator, same version and schema.
            metadata = old['routes'][route]['metadata']
            if route == 'facility-nuclear-outages':
                metadata = recording['requests'][0]['response']
            return httpx.Response(200, json={'response': metadata, 'apiVersion': '2.1.14'})
        params = dict(request.url.params)
        assert params.pop('api_key') == KEY
        offset = int(params['offset'])
        requested_offsets[route].append(offset)
        page = next(r for r in recording['requests'] if r['route'] == route and r['suffix'] == '/data/'
                    and r['params'].get('offset') == offset and 'facets[facility][]' not in r['params'])
        assert params == {k: str(v) for k, v in page['params'].items()}
        return httpx.Response(200, json={'response': page['response'], 'apiVersion': page['api_version']})
    events = []
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if mode == 'window':
            result = extraction.extract(client, KEY, START, END, tmp_path, page_size=200, attempts=1, emit=events.append)
        else:
            result = refresh.refresh(client, KEY, END, tmp_path, start=START, page_size=200, attempts=1, emit=events.append)
    assert requested_offsets['facility-nuclear-outages'] == [0, 110]
    assert requested_offsets['generator-nuclear-outages'] == [0]
    assert requested_offsets['us-nuclear-outages'] == [0]
    assert list(tmp_path.rglob('current.json'))
    assert not list(tmp_path.rglob('.staging-*'))
    assert result['validation']['source_completeness'] == 'unverified'
    assert result['validation']['facility_count_exception'] is True
    assert result['routes']['facility-nuclear-outages']['reported_total'] == 190
    assert result['routes']['facility-nuclear-outages']['received_rows'] == 110
    if mode == 'incremental':
        manifest, _, _ = refresh.load_snapshot(tmp_path)
        assert manifest['ingestion_run']['validation'] == result['validation']
        assert set(manifest['watermarks'].values()) == {str(END)}
