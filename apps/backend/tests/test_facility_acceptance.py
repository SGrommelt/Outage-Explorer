"""Recorded full-window replay and explicitly mutated negative controls; no live HTTP."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import gzip
import json
from pathlib import Path

import httpx
import pytest

from outage_explorer import extract_eia as extraction, refresh_eia as refresh, refresh_jobs

START, END = date(2026, 9, 22), date(2026, 10, 21)
N, F, G = extraction.ROUTES


@pytest.fixture
def evidence():
    path = Path(__file__).resolve().parents[3] / 'docs/evidence/eia-real-30-days-source.json.gz'
    return json.loads(gzip.decompress(path.read_bytes()))


def client_for(evidence, fault=None):
    """Original pages on positive replay; mutations are synthetic negative controls."""
    source = deepcopy(evidence)
    if fault in ('missing_facility', 'duplicate', 'mw', 'missing_day'):
        rows = source['routes'][F]['pages'][0]['response']['data']
        if fault == 'missing_facility': rows.pop(0)
        elif fault == 'duplicate': rows[1] = deepcopy(rows[0])
        elif fault == 'mw': rows[0]['outage'] = str(Decimal(rows[0]['outage']) + 1)
        else:
            for p in source['routes'][F]['pages']:
                p['response']['data'] = [r for r in p['response']['data'] if r['period'] != str(START)]
    if fault in ('missing_generator', 'duplicate_generator'):
        body = source['routes'][G]['pages'][0]['response']
        if fault == 'missing_generator':
            body['data'].pop(0)
            for p in source['routes'][G]['pages']: p['response']['total'] = '2849'
        else: body['data'][1] = deepcopy(body['data'][0])
    if fault == 'orphan':
        source['routes'][G]['pages'][0]['response']['data'][0]['facility'] = 'unknown-parent'
    if fault == 'offsetting':
        rows = source['routes'][F]['pages'][0]['response']['data']
        rows[0]['outage'] = str(Decimal(rows[0]['outage']) - 1)
        rows[1]['outage'] = str(Decimal(rows[1]['outage']) + 1)
    if fault == 'national_mw':
        source['routes'][N]['pages'][0]['response']['data'][0]['outage'] = '0'
    if fault == 'period_order':
        source['routes'][F]['pages'][0]['response']['data'].reverse()
    offsets = {route: {} for route in extraction.ROUTES}
    for route in extraction.ROUTES:
        offset = 0
        for p in source['routes'][route]['pages']:
            body = p['response']
            if fault == 'total' and route == F: body['total'] = '2851'
            if fault == 'generator_total' and route == G: body['total'] = '2851'
            if fault == 'national_total' and route == N: body['total'] = '31'
            offsets[route][offset] = body
            offset += len(body['data'])
    def handler(request):
        route = request.url.path.split('/')[3]
        if request.url.path.endswith('/data/'):
            offset = int(request.url.params['offset'])
            if fault == 'network_terminal' and route == F and offset == 1650:
                return httpx.Response(503)
            body = offsets[route][offset]
            if fault == 'unstable_total' and route == F and offset:
                body = {**body, 'total': '2851'}
        else:
            body = source['routes'][route]['metadata']['response']
        return httpx.Response(200, json={'response': body, 'apiVersion': '2.1.14'})
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(client, root, mode, **kwargs):
    options = dict(page_size=1000, attempts=1, emit=lambda _: None, **kwargs)
    if mode == 'window':
        return extraction.extract(client, 'offline', START, END, root, **options)
    return refresh.refresh(client, 'offline', END, root, start=START, **options)


@pytest.mark.parametrize('mode', ['window', 'incremental'])
def test_recorded_30_day_publication_is_valid_but_not_certified(evidence, tmp_path, mode):
    with client_for(evidence) as client:
        result = run(client, tmp_path, mode)
    assert [result['routes'][r]['records'] for r in extraction.ROUTES] == [30, 1650, 2850]
    assert result['routes'][F]['reported_total'] == 2850
    assert result['routes'][F]['terminal_empty_offset'] == 1650
    assert result['validation']['facility_count_exception'] is True
    assert result['validation']['source_completeness'] == 'unverified'
    if mode == 'incremental':
        manifest, rows, version = refresh.load_snapshot(tmp_path)
        assert manifest['ingestion_run']['published_snapshot_version'] == version
        assert manifest['ingestion_run']['validation'] == result['validation']
        assert set(manifest['watermarks'].values()) == {str(END)}
        assert len(rows[F]) == 1650
        summary = refresh_jobs.RefreshJobs._success(None, result)
        assert summary['status'] == 'succeeded' and 'completeness is unverified' in summary['warning']
        assert manifest['ingestion_run']['routes'][F]['acceptance']['received_rows'] == 1650


@pytest.mark.parametrize('mode', ['window', 'incremental'])
@pytest.mark.parametrize('fault', ['missing_facility', 'duplicate', 'mw', 'missing_day', 'total',
                                   'generator_total', 'network_terminal', 'unstable_total', 'missing_generator',
                                   'duplicate_generator', 'national_mw', 'period_order', 'orphan', 'offsetting', 'national_total'])
def test_fault_preserves_published_snapshot(evidence, tmp_path, mode, fault):
    with client_for(evidence) as client:
        run(client, tmp_path, mode)
    root = tmp_path if mode == 'incremental' else tmp_path / f'{START}_{END}'
    before = (root / 'current.json').read_bytes()
    with client_for(evidence, fault) as client:
        if mode == 'incremental':
            with pytest.raises(extraction.ExtractionError):
                refresh.refresh(client, 'offline', END, root, overlap_days=366,
                                page_size=1000, attempts=1, emit=lambda _: None)
        else:
            with pytest.raises(extraction.ExtractionError):
                run(client, root.parent, mode)
    assert (root / 'current.json').read_bytes() == before
    if mode == 'incremental':
        assert set(refresh.load_snapshot(root)[0]['watermarks'].values()) == {str(END)}


def test_recorded_full_overlap_preserves_metadata_on_recovery(evidence, tmp_path, monkeypatch):
    with client_for(evidence) as client:
        first = run(client, tmp_path, 'incremental')
    _, before, _ = refresh.load_snapshot(tmp_path)
    replace = refresh.os.replace
    def interrupt(src, dst):
        replace(src, dst)
        if Path(dst).name == 'current.json': raise KeyboardInterrupt
    monkeypatch.setattr(refresh.os, 'replace', interrupt)
    with client_for(evidence) as client, pytest.raises(KeyboardInterrupt):
        refresh.refresh(client, 'offline', END, tmp_path, overlap_days=366,
                        page_size=1000, attempts=1, emit=lambda _: None)
    monkeypatch.setattr(refresh.os, 'replace', replace)
    current, rows, version = refresh.load_snapshot(tmp_path)
    assert rows == before and current['validation'] == first['validation']
    # Remove mirror: next zero-overlap no-op must recover it from the committed manifest.
    mirror = tmp_path / 'ingestion-runs' / f'{version}.json'
    mirror.unlink()
    with client_for(evidence) as client:
        refresh.refresh(client, 'offline', END, tmp_path, overlap_days=0, emit=lambda _: None)
    assert json.loads(mirror.read_text())['validation'] == current['validation']


@pytest.mark.parametrize('fault', ['claim', 'missing', 'audit'])
def test_corrupt_acceptance_metadata_is_not_recovered_or_hidden(evidence, tmp_path, fault):
    with client_for(evidence) as client:
        run(client, tmp_path, 'incremental')
    run_path = tmp_path / json.loads((tmp_path / 'current.json').read_text())['run']
    manifest_path = run_path / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if fault == 'claim': manifest['validation']['source_completeness'] = 'certified'
    elif fault == 'missing': del manifest['validation']
    else: manifest['ingestion_run']['routes'][F]['acceptance']['reported_total'] = 1650
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(extraction.ExtractionError, match='acceptance'):
        refresh.load_snapshot(tmp_path)


@pytest.fixture
def synthetic_routes():
    """Explicitly synthetic balanced observations: one facility, two generators/day."""
    result = {route: [] for route in extraction.ROUTES}
    for i in range(5):
        day = str(date(2026, 1, 1) + timedelta(days=i))
        units = {name + '-units': unit for name, unit in extraction.MEASURES.items()}
        parent = {'period': day, 'capacity': '150', 'outage': '15',
                  'percentOutage': '10', **units}
        result[N].append(parent.copy())
        result[F].append({**parent, 'facility': 'plant-A', 'facilityName': 'Synthetic plant'})
        for generator, capacity, outage in [('unit-A', '100', '10'), ('unit-B', '50', '5')]:
            result[G].append({**parent, 'facility': 'plant-A', 'facilityName': 'Synthetic plant',
                              'generator': generator, 'capacity': capacity, 'outage': outage})
    return result


def filtered_transport(evidence, rows, expected_start, expected_end, requests):
    """Enforce every request's dates and return only that synthetic date window."""
    def handler(request):
        route = request.url.path.split('/')[3]
        if request.url.path.endswith('/data/'):
            params = request.url.params
            assert params['start'] == str(expected_start)
            assert params['end'] == str(expected_end)
            assert params['frequency'] == 'daily'
            assert [params[f'sort[{i}][column]'] for i in range(len(extraction.IDENTIFIERS[route]) + 1)] == [
                'period', *extraction.IDENTIFIERS[route]]
            assert all(params[f'sort[{i}][direction]'] == 'asc' for i in range(len(extraction.IDENTIFIERS[route]) + 1))
            selected = sorted((r for r in rows[route] if params['start'] <= r['period'] <= params['end']),
                              key=lambda r: (r['period'], *(r[k] for k in extraction.IDENTIFIERS[route])))
            offset, length = int(params['offset']), int(params['length'])
            # Deliberately mimic the Facility count discrepancy in synthetic data.
            total = (sum(params['start'] <= r['period'] <= params['end'] for r in rows[G])
                     if route == F else len(selected))
            page = selected[offset:offset + length]
            requests.append({'route': route, 'start': params['start'], 'end': params['end'],
                             'offset': offset, 'length': length, 'returned': len(page), 'total': total})
            body = {'frequency': 'daily', 'dateFormat': 'YYYY-MM-DD', 'total': str(total), 'data': page}
        else:
            body = evidence['routes'][route]['metadata']['response']
        return httpx.Response(200, json={'response': body, 'apiVersion': '2.1.14'})
    return httpx.MockTransport(handler)


def test_facility_alone_requires_terminal_page_with_one_page_budget(evidence, synthetic_routes, tmp_path):
    """National and Generator finish; Facility must request one additional empty page."""
    start, end = date(2026, 1, 1), date(2026, 1, 3)
    requests, events = [], []
    transport = filtered_transport(evidence, synthetic_routes, start, end, requests)
    with httpx.Client(transport=transport) as client:
        with pytest.raises(extraction.ExtractionError, match='failed validation'):
            refresh.refresh(client, 'offline', end, tmp_path, start=start, page_size=10,
                            max_pages=1, attempts=1, emit=events.append)
    assert not (tmp_path / 'current.json').exists()
    assert {e['route'] for e in events if e['event'] == 'route_complete'} == {N, G}
    assert [(r['route'], r['offset'], r['returned'], r['total']) for r in requests] == [
        (N, 0, 3, 3), (F, 0, 3, 6), (G, 0, 6, 6)]
    audit = json.loads(next((tmp_path / 'ingestion-runs').glob('*.json')).read_text())
    assert audit['failures'] == [
        F + ': Page limit reached before completion; nothing published.',
        'Refresh failed validation; no snapshot published.',
    ]
    assert audit['routes'][F]['status'] == 'failed'
    assert audit['routes'][F]['fetched_rows'] == 3
    assert all(audit['routes'][r]['status'] == 'validated' for r in (N, G))
    assert all(r['watermark_after'] is None for r in audit['routes'].values())
    # Same source, one extra page: only Facility makes another request, at offset 3.
    requests.clear()
    with httpx.Client(transport=transport) as client:
        published = refresh.refresh(client, 'offline', end, tmp_path, start=start, page_size=10,
                                    max_pages=2, attempts=1, emit=lambda _: None)
    assert [(r['offset'], r['returned'], r['total']) for r in requests if r['route'] == F] == [
        (0, 3, 6), (3, 0, 6)]
    assert [(r['offset'], r['returned']) for r in requests if r['route'] == G] == [(0, 6)]
    assert [(r['offset'], r['returned']) for r in requests if r['route'] == N] == [(0, 3)]
    assert published['routes'][F]['terminal_empty_offset'] == 3


def test_partial_overlap_exception_counts_and_recovery(evidence, synthetic_routes, tmp_path, monkeypatch):
    start, old_end, end = date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 5)
    requests = []
    with httpx.Client(transport=filtered_transport(evidence, synthetic_routes, start, old_end, requests)) as client:
        refresh.refresh(client, 'offline', old_end, tmp_path, start=start, page_size=3,
                        attempts=1, emit=lambda _: None)
    _, old_rows, old_version = refresh.load_snapshot(tmp_path)
    historical = {r: {k: deepcopy(v) for k, v in rows.items() if k[0] == start}
                  for r, rows in old_rows.items()}
    # The source changes outside the next fetched window: these rows must not leak in.
    for route in (N, F):
        synthetic_routes[route][0]['outage'] = '30'
        synthetic_routes[route][0]['percentOutage'] = '20'
    for row in synthetic_routes[G][:2]:
        row['outage'] = str(Decimal(row['outage']) * 2)
        row['percentOutage'] = '20'
    requests.clear()
    expected_start = date(2026, 1, 2)  # Jan 3 watermark + 1 day - 2-day overlap.
    transport = filtered_transport(evidence, synthetic_routes, expected_start, end, requests)
    replace, atomic = refresh.os.replace, refresh.atomic_json
    def interrupt_after_pointer(src, dst):
        replace(src, dst)
        if Path(dst).name == 'current.json': raise KeyboardInterrupt
    def fail_published_mirror(path, value):
        if value.get('status') == 'published': raise OSError('simulated audit mirror failure')
        return atomic(path, value)
    monkeypatch.setattr(refresh.os, 'replace', interrupt_after_pointer)
    monkeypatch.setattr(refresh, 'atomic_json', fail_published_mirror)
    with httpx.Client(transport=transport) as client, pytest.raises(KeyboardInterrupt):
        refresh.refresh(client, 'offline', end, tmp_path, overlap_days=2, page_size=3,
                        attempts=1, emit=lambda _: None)
    monkeypatch.setattr(refresh.os, 'replace', replace)
    monkeypatch.setattr(refresh, 'atomic_json', atomic)
    manifest, rows, version = refresh.load_snapshot(tmp_path)
    assert version != old_version
    assert requests and {r['route'] for r in requests} == set(extraction.ROUTES)
    assert {(r['start'], r['end']) for r in requests} == {(str(expected_start), str(end))}
    assert [(r['offset'], r['returned'], r['total']) for r in requests if r['route'] == F] == [
        (0, 3, 8), (3, 1, 8), (4, 0, 8)]
    assert [(r['offset'], r['returned'], r['total']) for r in requests if r['route'] == G] == [
        (0, 3, 8), (3, 3, 8), (6, 2, 8)]
    audit = manifest['ingestion_run']
    assert audit['requested_start'] == str(expected_start) and audit['requested_end'] == str(end)
    assert manifest['start'] == str(start) and manifest['end'] == str(end)
    assert audit['base_snapshot_version'] == old_version and audit['published_snapshot_version'] == version
    assert set(manifest['watermarks'].values()) == {str(end)}
    for route, incoming, cumulative in [(N, 4, 5), (F, 4, 5), (G, 8, 10)]:
        entry, route_audit = manifest['routes'][route], audit['routes'][route]
        assert entry['received_rows'] == entry['unique_rows'] == route_audit['fetched_rows'] == incoming
        assert entry['records'] == route_audit['merged_rows'] == len(rows[route]) == cumulative
        assert route_audit['acceptance'] == {**entry, 'records': incoming}
        assert {k: v for k, v in rows[route].items() if k[0] < expected_start} == historical[route]
    assert manifest['routes'][F]['reported_total'] == manifest['routes'][G]['received_rows'] == 8
    assert manifest['routes'][F]['reported_total'] != manifest['routes'][G]['records']
    validation = manifest['validation']
    assert validation == audit['validation']
    assert validation['policy'] == extraction.FACILITY_POLICY
    assert validation['facility_count_exception'] is True
    assert validation['source_completeness'] == 'unverified'
    assert validation['caveat'] == extraction.SOURCE_CAVEAT
    assert all(validation[k] is True for k in (
        'facility_generator_keys_match', 'facility_generator_mw_match', 'daily_mw_match'))
    mirror = tmp_path / 'ingestion-runs' / f'{version}.json'
    assert json.loads(mirror.read_text())['status'] == 'running'
    # Recovery-only no-op must not fetch anything or alter the authoritative snapshot.
    pointer = (tmp_path / 'current.json').read_bytes()
    requests.clear()
    def no_network(_): pytest.fail('Recovery/no-op unexpectedly made an HTTP request')
    with httpx.Client(transport=httpx.MockTransport(no_network)) as client:
        outcome = refresh.refresh(client, 'offline', end, tmp_path, overlap_days=0, emit=lambda _: None)
    recovered_manifest, recovered_rows, recovered_version = refresh.load_snapshot(tmp_path)
    assert outcome['status'] == 'no_op'
    assert requests == [] and (tmp_path / 'current.json').read_bytes() == pointer
    assert recovered_manifest == manifest and recovered_rows == rows and recovered_version == version
    assert json.loads(mirror.read_text()) == audit
