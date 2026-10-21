"""Isolated offline collection/replay tests; no production publications."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import gzip
import json
from pathlib import Path

import httpx
import pytest

from outage_explorer import analysis_eia as analysis
from outage_explorer.explore_eia import ROUTES
from outage_explorer.extract_eia import ExtractionError, MEASURES

END = date(2026, 10, 20)
START = END - timedelta(days=29)
KEY = 'offline-analysis-credential'


def rows(route):
    result = []
    for i in range(30):
        identities = [({}, '300', '30')]
        if route != ROUTES[0]:
            identities = [({'facility': '001', 'facilityName': 'Test'}, '300', '30')]
        if route == ROUTES[2]:
            identities = [({'facility': '001', 'facilityName': 'Test', 'generator': str(n)}, capacity, outage)
                          for n, capacity, outage in [(1, '100', '10'), (2, '200', '20')]]
        for identity, capacity, outage in identities:
            result.append({'period': str(START + timedelta(days=i)), **identity, 'capacity': capacity,
                           'outage': outage, 'percentOutage': '10', **{m+'-units': u for m, u in MEASURES.items()}})
    return result


@pytest.fixture
def metadata():
    fixture = json.loads((Path(__file__).parent / 'fixtures/eia_nuclear_sample.json').read_text())
    return {r: fixture['routes'][r]['metadata'] for r in ROUTES}


def handler(metadata, *, fail=None, retry=None):
    calls = []
    def respond(request):
        calls.append(request)
        route = request.url.path.removeprefix('/v2/nuclear-outages/').split('/')[0]
        assert request.url.params['api_key'] == KEY
        if retry is not None and len(calls) == 1:
            return httpx.Response(retry, headers={'Retry-After': '0'})
        if fail == route and request.url.path.endswith('/data/'):
            return httpx.Response(503)
        if request.url.path.endswith('/data/'):
            assert request.url.params['start'] == str(START)
            assert request.url.params['end'] == str(END)
            offset = int(request.url.params['offset'])
            values = rows(route)
            body = {'frequency': 'daily', 'dateFormat': 'YYYY-MM-DD', 'total': str(60 if route == ROUTES[1] else len(values)), 'data': values[offset:offset + 1000]}
        else:
            body = metadata[route]
        return httpx.Response(200, json={'response': body, 'apiVersion': '2.1.14', 'request': {'api_key': KEY}})
    return respond, calls


@pytest.fixture
def source(tmp_path, metadata):
    respond, _ = handler(metadata)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        return analysis.collect(client, KEY, tmp_path / 'eia-analysis/source.json.gz', end=END, sleep=lambda _: None)


def test_bounded_collection_redaction_and_offline_replay(tmp_path, metadata):
    respond, calls = handler(metadata)
    output = tmp_path / 'eia-analysis/source.json.gz'
    sentinel = tmp_path / 'production/current.json'
    sentinel.parent.mkdir(); sentinel.write_text('unchanged')
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        analysis.collect(client, KEY, output, end=END, sleep=lambda _: None)
        assert client.event_hooks['request'] == []
    raw = gzip.decompress(output.read_bytes())
    assert KEY.encode() not in raw and b'api_key' not in raw
    saved = json.loads(raw)
    assert saved['request_count'] == len(calls) == 9
    result = analysis.analyse(saved)
    assert result['production_eligible'] is False
    assert len(result['daily']) == len(result['facilities']) == 30
    assert all(day['status'] == 'equal' and day['generator_minus_facility_mw'] == 0 for day in result['daily'])
    assert result['routes'][ROUTES[1]]['received_rows'] == 30
    assert result['routes'][ROUTES[1]]['reported_totals'] == [60, 60]
    assert result['routes'][ROUTES[1]]['count_agrees'] is False
    assert all(route['days_covered'] == 30 for route in result['routes'].values())
    assert not any(result['anomalies'].values())  # Never invent three anomalies to fill a quota.
    assert sentinel.read_text() == 'unchanged'
    assert not list(output.parent.rglob('current.json'))
    assert not list(output.parent.rglob('ingestion-runs'))


@pytest.mark.parametrize('status', [429, 503])
def test_bounded_retry_and_request_accounting(tmp_path, metadata, status):
    respond, calls = handler(metadata, retry=status)
    delays = []
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = analysis.collect(client, KEY, tmp_path / 'eia-analysis/source.json.gz', end=END, sleep=delays.append)
    assert len(calls) == source['request_count'] == 10
    assert len(source['retries']) == 1 and 1 in delays


def test_failed_route_retains_valid_partial_evidence(tmp_path, metadata):
    respond, calls = handler(metadata, fail=ROUTES[2])
    path = tmp_path / 'eia-analysis/source.json.gz'
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        analysis.collect(client, KEY, path, end=END, sleep=lambda _: None)
    result = analysis.analyse(json.loads(gzip.decompress(path.read_bytes())))
    assert result['routes'][ROUTES[0]]['days_covered'] == 30
    assert result['routes'][ROUTES[2]]['days_covered'] == 0
    assert len(result['anomalies']['missing_dates']) == 30
    assert all(row['status'] == 'missing_level' for row in result['daily'])
    assert any('Retry budget exhausted' in failure for failure in result['routes'][ROUTES[2]]['failures'])
    assert len(calls) <= 36


def test_checksum_tampering_is_rejected(source):
    source['routes'][ROUTES[0]]['pages'][0]['response']['data'][0]['outage'] = '999'
    with pytest.raises(ExtractionError, match='checksum'):
        analysis.analyse(source)


@pytest.mark.parametrize('fault', ['duplicate', 'units', 'date', 'extra_field'])
def test_bad_records_withhold_ambiguous_analysis(source, fault):
    page = source['routes'][ROUTES[0]]['pages'][0]
    row = page['response']['data'][0]
    if fault == 'duplicate':
        page['response']['data'].append(deepcopy(row))
        page['response']['total'] = '31'
        source['routes'][ROUTES[0]]['pages'][1]['query']['offset'] = 31
    elif fault == 'units': row['capacity-units'] = 'unknown'
    elif fault == 'date': row['period'] = '2026-02-30'
    else: row['unexpected'] = 'schema drift'
    page['sha256'] = analysis.digest(page['response'])
    result = analysis.analyse(source)
    assert result['status'] == 'invalid_records_analysis_withheld'
    assert result['daily'] == [] and result['anomalies'] == {}


def test_range_anomaly_values_are_backed_by_exact_source_records(source):
    page = source['routes'][ROUTES[2]]['pages'][0]
    row = page['response']['data'][0]
    row.update(outage='-0.125', percentOutage='-0.125')
    page['sha256'] = analysis.digest(page['response'])
    result = analysis.analyse(source)
    flags = result['anomalies']['range_flags']
    assert len(flags) == 1
    flag = flags[0]
    assert flag['outage_date'] == START and flag['facility_id'] == row['facility'] and flag['generator_id'] == row['generator']
    assert flag['outage_mw'] == Decimal(row['outage'])
    assert flag['capacity_mw'] == Decimal(row['capacity'])
    assert flag['negative_outage'] is True and flag['percent_outside_range'] is True
    assert flag['category'] == 'review'  # Unusual source value, not proven erroneous.


def test_metadata_only_failure_and_budget_preserve_evidence(tmp_path, metadata):
    respond, calls = handler(metadata)
    path = tmp_path / 'eia-analysis/source.json.gz'
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        source = analysis.collect(client, KEY, path, end=END, sleep=lambda _: None, request_budget=2)
    assert len(calls) == 2
    assert analysis.analyse(source)['status'] == 'metadata_incomplete'
    assert path.is_file()


def test_outputs_cannot_target_production_or_overwrite_evidence(tmp_path, metadata):
    with pytest.raises(ExtractionError, match='eia-analysis'):
        analysis.save_source(tmp_path / 'production/current.json', {}, KEY)
    path = tmp_path / 'eia-analysis/source.json.gz'; path.parent.mkdir(); path.write_bytes(b'keep')
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('No network expected'))) as client:
        with pytest.raises(ExtractionError, match='already exists'):
            analysis.collect(client, KEY, path)
    assert path.read_bytes() == b'keep'


def test_resume_fetches_only_missing_offsets_and_retains_failure_history(tmp_path, metadata, source):
    partial = deepcopy(source)
    entry = partial['routes'][ROUTES[2]]
    entry['pages'] = entry['pages'][:1]
    entry['pages'][0]['response']['data'] = entry['pages'][0]['response']['data'][:30]
    entry['pages'][0]['sha256'] = analysis.digest(entry['pages'][0]['response'])
    entry['failures'] = ['Retry budget exhausted (network, 2 attempts).']
    respond, calls = handler(metadata)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        resumed = analysis.collect(client, KEY, tmp_path / 'eia-analysis/resumed.json.gz', sleep=lambda _: None,
                                   resume_source=partial)
    assert [int(call.url.params['offset']) for call in calls] == [30, 60]
    assert all('generator-nuclear-outages/data/' in call.url.path for call in calls)
    assert resumed['routes'][ROUTES[2]]['failure_history'] == entry['failures']
    assert resumed['routes'][ROUTES[2]]['failures'] == []
    assert entry['failures']  # The original recording was not changed.
    result = analysis.analyse(resumed)
    assert all(r['days_covered'] == 30 for r in result['routes'].values())
    assert not result['anomalies']['missing_dates']


def test_unadvertised_filters_or_changed_query_scope_are_rejected(source):
    source['routes'][ROUTES[1]]['pages'][0]['query']['facets[facility][]'] = '001'
    with pytest.raises(ExtractionError, match='offsets/date filters'):
        analysis.analyse(source)


@pytest.mark.parametrize('command', ['analyse', 'inspect'])
def test_cli_replays_without_credentials_or_network(source, tmp_path, monkeypatch, command):
    path = tmp_path / 'eia-analysis/source.json.gz'
    analysis.save_source(path, source, KEY)
    monkeypatch.setattr(analysis, 'load_api_key', lambda _: pytest.fail('Offline replay must not load credentials'))
    monkeypatch.setattr(analysis.httpx, 'Client', lambda **_: pytest.fail('Offline replay must not access network'))
    output = path.with_name('analysis.json')
    assert analysis.main([command, '--input', str(path), '--output', str(output)]) == 0
    result = json.loads(output.read_text())
    assert result['production_eligible'] is False
    if command == 'analyse':
        assert len(result['daily']) == 30
    else:
        assert result['classification'] == 'candidate_inventory_not_confirmed_source_anomalies'
    assert analysis.main([command, '--input', str(path), '--output', str(tmp_path / 'production/current.json')]) == 1
    assert not (tmp_path / 'production').exists()


def test_retained_real_window_reproduces_documented_results_offline():
    import hashlib
    directory = Path(__file__).resolve().parents[3] / 'docs/evidence'
    path = directory / 'eia-real-30-days-source.json.gz'
    summary = json.loads((directory / 'eia-real-30-days-summary.json').read_text())
    assert hashlib.sha256(path.read_bytes()).hexdigest() == summary['source_file_sha256']
    content = gzip.decompress(path.read_bytes())
    assert b'api_key' not in content
    result = analysis.analyse(json.loads(content))
    assert (result['start'], result['end']) == ('2026-09-22', '2026-10-21')
    assert [result['routes'][r]['validated_rows'] for r in ROUTES] == [30, 1650, 2850]
    assert all(r['unique_keys'] == r['received_rows'] and r['days_covered'] == 30 for r in result['routes'].values())
    assert all(row['status'] == 'equal' for row in result['daily'] + result['facilities'])
    assert json.loads(analysis.encoded(result['daily'])) == summary['daily']
    assert len(result['facilities']) == summary['facility_comparisons'] == 1650
    assert result['routes'] == summary['routes']
    assert {kind:len(rows) for kind,rows in result['anomalies'].items()} == summary['anomaly_counts']
    assert not any(result['anomalies'].values())
    assert summary['source_data_anomalies_found'] == 0
    assert summary['three_anomaly_requirement_met'] is False


def rehash_pages(source):
    for entry in source['routes'].values():
        for page in entry['pages']:
            page['sha256'] = analysis.digest(page['response'])


def test_temporal_capacity_changes_are_decomposed_not_asserted_as_defects(source):
    row = source['routes'][ROUTES[2]]['pages'][0]['response']['data'][2]
    row['capacity'] = '125'
    rehash_pages(source)
    result = analysis.inspect_temporal(source)
    changes = [r for r in result['capacity_decomposition'] if r['route'] == ROUTES[2]]
    assert [r['total_change_mw'] for r in changes] == [Decimal(25), Decimal(-25)]
    assert all(r['added_key_count'] == r['removed_key_count'] == 0 and r['changed_stable_keys'] == 1 for r in changes)
    assert result['membership_changes'] == []
    assert all(r['classification'] == 'reported_capacity_change_not_proven_defect' for r in result['capacity_changes'])


def test_temporal_membership_and_nullable_labels_are_separate_candidates(source):
    row = source['routes'][ROUTES[2]]['pages'][0]['response']['data'][2]
    row['facility'] = '002'
    row['facilityName'] = None
    rehash_pages(source)
    result = analysis.inspect_temporal(source)
    assert len(result['membership_changes']) == 2
    event = result['membership_changes'][0]
    assert event['added_keys'] == [('002', '1')] and event['removed_keys'] == [('001', '1')]
    assert len(result['missing_labels']) == 1
    assert all(r['added_capacity_mw'] == r['removed_capacity_mw'] == Decimal(100) for r in result['capacity_decomposition'])
    assert result['production_eligible'] is False


def test_percentage_boundary_is_exact_and_ordinary_rounding_is_not_flagged(source):
    rows = source['routes'][ROUTES[0]]['pages'][0]['response']['data']
    rows[0].update(capacity='200', outage='20.01', percentOutage='10.00')
    rows[1].update(capacity='200', outage='20.012', percentOutage='10.00')
    rehash_pages(source)
    result = analysis.inspect_temporal(source)
    assert len(result['percentage_candidates']) == 1
    candidate = result['percentage_candidates'][0]
    assert candidate['outage_date'] == START + timedelta(days=1)
    assert candidate['calculated_percent_outage'] == Decimal('10.006')
    assert candidate['gap_percentage_points'] == Decimal('-0.006')
    assert 'review' in candidate['classification']  # Source rounding contract remains unverified.


def test_temporal_does_not_bridge_missing_dates_or_ignore_checksum_faults(source):
    entry = source['routes'][ROUTES[0]]
    del entry['pages'][0]['response']['data'][1]
    for page in entry['pages']:
        page['response']['total'] = '29'
    entry['pages'][1]['query']['offset'] = 29
    rehash_pages(source)
    result = analysis.inspect_temporal(source)
    assert result['baseline_anomaly_counts']['missing_dates'] == 1
    assert len(result['skipped_intervals']) == 1
    entry['pages'][0]['response']['data'][0]['capacity'] = '100'
    with pytest.raises(ExtractionError, match='checksum'):
        analysis.inspect_temporal(source)


def test_real_temporal_inspection_has_no_membership_or_percentage_defect():
    directory = Path(__file__).resolve().parents[3] / 'docs/evidence'
    source = json.loads(gzip.decompress((directory / 'eia-real-30-days-source.json.gz').read_bytes()))
    result = analysis.inspect_temporal(source)
    summary = json.loads((directory / 'eia-real-30-days-temporal-summary.json').read_text())
    assert json.loads(analysis.encoded(result['capacity_decomposition'])) == summary['capacity_decomposition']
    assert [row['changed_stable_keys'] for row in result['capacity_decomposition']] == [1, 47, 78]
    assert all(row['total_change_mw'] == row['stable_key_change_mw'] == Decimal('2436.2') for row in result['capacity_decomposition'])
    assert all(row['added_key_count'] == row['removed_key_count'] == 0 for row in result['capacity_decomposition'])
    assert all(row['outage_date'] == date(2026, 10, 1) for row in result['capacity_changes'])
    for name in ('membership_changes', 'label_changes', 'cross_grain_label_mismatches', 'missing_labels', 'percentage_candidates', 'undefined_ratios'):
        assert result[name] == []
    assert result['source_percentage_decimal_places'] == summary['source_percentage_decimal_places']
    assert json.loads(analysis.encoded(result['maximum_percentage_gaps'])) == summary['maximum_percentage_gaps']
    assert len(result['capacity_changes']) == 126  # One coordinated step across three grains, not 126 anomalies.
