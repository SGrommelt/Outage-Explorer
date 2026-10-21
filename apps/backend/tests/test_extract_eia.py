"""Offline extraction behavior tests using public recorded rows and synthetic faults."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path

import httpx
import pyarrow.parquet as pq
import pytest

from outage_explorer import extract_eia as eia

START = date(2026, 10, 18)
END = date(2026, 10, 20)
KEY = 'fake-extraction-secret'


@pytest.fixture
def dataset():
    recorded = json.loads((Path(__file__).parent / 'fixtures/eia_nuclear_sample.json').read_text())
    result = {}
    for route in eia.ROUTES:
        item = recorded['routes'][route]
        sample = [row for page in item['pages'] for row in page['response']['data']]
        rows = []
        for day in range(3):
            period = (START + timedelta(days=day)).isoformat()
            source = sample[:1] if route == 'us-nuclear-outages' else sample
            for row in source:
                rows.append({**row, 'period': period})
        result[route] = {'metadata': item['metadata'], 'rows': rows}
    return result


def handler_for(dataset, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        assert request.url.params['api_key'] == KEY
        route = request.url.path.removeprefix('/v2/nuclear-outages/').split('/')[0]
        if not request.url.path.endswith('/data/'):
            body = dataset[route]['metadata']
        else:
            params = request.url.params
            assert params['start'] == START.isoformat()
            assert params['end'] == END.isoformat()
            assert params['frequency'] == 'daily'
            for i, field in enumerate(('period', *eia.IDENTIFIERS[route])):
                assert params[f'sort[{i}][column]'] == field
                assert params[f'sort[{i}][direction]'] == 'asc'
            assert [params[f'data[{i}]'] for i in range(3)] == list(eia.MEASURES)
            offset, length = int(params['offset']), int(params['length'])
            rows = dataset[route]['rows']
            body = {'frequency': 'daily', 'dateFormat': 'YYYY-MM-DD', 'total': str(len(rows)), 'data': rows[offset:offset + length]}
        return httpx.Response(200, json={'response': body, 'apiVersion': '2.1.14', 'request': {'params': {'api_key': KEY}}})
    return handler


def run(dataset, output, *, handler=None, **options):
    with httpx.Client(transport=httpx.MockTransport(handler or handler_for(dataset))) as client:
        return eia.extract(client, KEY, START, END, output, page_size=2, emit=lambda _: None, **options)


def current(output):
    window = output / f'{START}_{END}'
    pointer = json.loads((window / 'current.json').read_text())
    return window / pointer['run']


def fingerprint(output):
    run_dir = current(output)
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in run_dir.iterdir()}


def test_pagination_numeric_conversion_schemas_and_repeat_idempotence(dataset, tmp_path):
    calls = []
    manifest = run(dataset, tmp_path, handler=handler_for(dataset, calls))
    assert [manifest['routes'][r]['records'] for r in eia.ROUTES] == [3, 18, 18]
    assert [manifest['routes'][r]['pages'] for r in eia.ROUTES] == [2, 9, 9]
    for route in eia.ROUTES:
        table = pq.read_table(current(tmp_path) / f'{route}.parquet')
        assert table.schema == eia.schema_for(route)
        assert table.num_rows == len(dataset[route]['rows'])
        first = table.to_pylist()[0]
        assert isinstance(first['period'], date)
        assert isinstance(first['capacity'], Decimal)
        if eia.IDENTIFIERS[route]:
            assert first['facility'] == '46'
    previous = current(tmp_path)
    previous_parquet = {p.name: p.read_bytes() for p in previous.glob('*.parquet')}
    run(dataset, tmp_path)
    assert current(tmp_path) != previous
    assert previous.exists()
    assert previous_parquet == {p.name: p.read_bytes() for p in current(tmp_path).glob('*.parquet')}
    assert not list((tmp_path / f'{START}_{END}').glob('.staging-*'))
    assert all(int(r.url.params['length']) <= 2 for r in calls if r.url.path.endswith('/data/'))


@pytest.mark.parametrize('field,value', [
    ('period', '2026-02-30'), ('period', '2026-10-21'), ('period', None),
    ('capacity', None), ('capacity', ''), ('capacity', 'bad'), ('capacity', True),
    ('outage', 'NaN'), ('percentOutage', 'Infinity'), ('capacity', '1e18'),
    ('capacity', '0.0000001'), ('capacity-units', 'kilowatts'),
    ('facility', ''), ('facility', 46), ('facility', ' 46'), ('facilityName', 42),
])
def test_invalid_data_fails_explicitly(dataset, tmp_path, field, value):
    dataset['facility-nuclear-outages']['rows'][0][field] = value
    with pytest.raises(eia.ExtractionError):
        run(dataset, tmp_path)
    assert not list(tmp_path.rglob('current.json'))
    assert not list(tmp_path.rglob('*.parquet'))


@pytest.mark.parametrize('field', ['capacity', 'capacity-units', 'generator', 'period'])
def test_missing_required_fields(dataset, tmp_path, field):
    del dataset['generator-nuclear-outages']['rows'][0][field]
    with pytest.raises(eia.ExtractionError):
        run(dataset, tmp_path)
    assert not list(tmp_path.rglob('current.json'))


def test_optional_name_and_identifier_preservation(dataset):
    row = deepcopy(dataset['generator-nuclear-outages']['rows'][0])
    del row['facilityName']
    row['generator'] = '001A'
    result, key = eia.validate_record(row, 'generator-nuclear-outages', START, END)
    assert result['facilityName'] is None
    assert key[-1] == '001A'


@pytest.mark.parametrize('conflict', [False, True])
def test_duplicates_across_pages_rejected_without_replacing_valid_output(dataset, tmp_path, conflict):
    run(dataset, tmp_path)
    before = current(tmp_path), fingerprint(tmp_path)
    duplicate = deepcopy(dataset['generator-nuclear-outages']['rows'][0])
    if conflict:
        duplicate['capacity'] = '1'
    dataset['generator-nuclear-outages']['rows'].insert(2, duplicate)
    with pytest.raises(eia.ExtractionError, match='Duplicate'):
        run(dataset, tmp_path)
    assert (current(tmp_path), fingerprint(tmp_path)) == before


@pytest.mark.parametrize('failure', ['interrupt', 'http', 'disk'])
def test_partial_failure_never_publishes_partial_snapshot(dataset, tmp_path, monkeypatch, failure):
    run(dataset, tmp_path)
    before = current(tmp_path), fingerprint(tmp_path)
    base = handler_for(dataset)

    def handler(request):
        if 'generator-nuclear-outages/data/' in request.url.path and request.url.params['offset'] == '2':
            if failure == 'interrupt':
                raise KeyboardInterrupt
            if failure == 'http':
                return httpx.Response(503, text=KEY)
        return base(request)

    if failure == 'disk':
        replace = eia.os.replace
        def fail_pointer(source, target):
            if Path(target).name == 'current.json':
                raise OSError(KEY)
            return replace(source, target)
        monkeypatch.setattr(eia.os, 'replace', fail_pointer)
    expected = KeyboardInterrupt if failure == 'interrupt' else OSError if failure == 'disk' else eia.ExtractionError
    with pytest.raises(expected):
        run(dataset, tmp_path, handler=handler, attempts=1)
    assert (current(tmp_path), fingerprint(tmp_path)) == before
    assert not list(tmp_path.rglob('.staging-*'))
    assert not list(tmp_path.rglob('.current-*.json'))


@pytest.mark.parametrize('fault', ['total_changed', 'empty_page', 'oversized', 'wrong_frequency', 'wrong_date_format', 'api_error', 'invalid_json', 'warning'])
def test_malformed_or_incomplete_pagination(dataset, tmp_path, fault):
    base = handler_for(dataset)
    def handler(request):
        response = base(request)
        if request.url.path.endswith('/data/'):
            payload = response.json()
            if fault == 'invalid_json':
                return httpx.Response(200, text='not-json-' + KEY)
            if fault == 'api_error':
                return httpx.Response(200, json={'error': KEY})
            if fault == 'warning':
                payload['warning'] = KEY
            body = payload['response']
            if fault == 'empty_page':
                body['data'] = []
            elif fault == 'oversized':
                body['data'] *= 3
            elif fault == 'wrong_frequency':
                body['frequency'] = 'monthly'
            elif fault == 'wrong_date_format':
                body['dateFormat'] = 'YYYY'
            elif fault == 'total_changed' and request.url.params['offset'] != '0':
                body['total'] = '99'
            return httpx.Response(200, json=payload)
        return response
    with pytest.raises(eia.ExtractionError) as exc:
        run(dataset, tmp_path, handler=handler)
    assert KEY not in str(exc.value)
    assert not list(tmp_path.rglob('current.json'))


@pytest.mark.parametrize('fault', ['empty', 'gap', 'page_limit', 'schema_drift', 'metadata_drift'])
def test_conservative_publication_guards(dataset, tmp_path, fault):
    if fault == 'empty':
        dataset['facility-nuclear-outages']['rows'] = []
    elif fault == 'gap':
        dataset['facility-nuclear-outages']['rows'] = dataset['facility-nuclear-outages']['rows'][:6]
    elif fault == 'schema_drift':
        dataset['facility-nuclear-outages']['rows'][0]['unexpected'] = 'value'
    elif fault == 'metadata_drift':
        dataset['facility-nuclear-outages']['metadata']['facets'] = []
    with pytest.raises(eia.ExtractionError):
        run(dataset, tmp_path, max_pages=1 if fault == 'page_limit' else 100)
    assert not list(tmp_path.rglob('current.json'))


@pytest.mark.parametrize('fault', [429, 500, 503, 'timeout', 'connect'])
def test_transient_errors_retry_with_exponential_backoff(dataset, tmp_path, fault):
    base = handler_for(dataset)
    count, delays, logs = 0, [], []
    def handler(request):
        nonlocal count
        count += 1
        if count < 4:
            if fault == 'timeout':
                raise httpx.ReadTimeout(KEY, request=request)
            if fault == 'connect':
                raise httpx.ConnectError(KEY, request=request)
            return httpx.Response(fault, text=KEY)
        return base(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        eia.extract(client, KEY, START, END, tmp_path, page_size=2, sleep=delays.append, emit=logs.append)
    assert delays == [1, 2, 4]
    assert sum(e['event'] == 'retry' for e in logs) == 3
    assert logs[-1]['event'] == 'extraction_complete'
    assert KEY not in json.dumps(logs)
    assert 'api_key=' not in json.dumps(logs)


@pytest.mark.parametrize('status', [401, 403, 404, 302])
def test_non_retryable_status(dataset, tmp_path, status):
    calls, delays = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=KEY)
    with pytest.raises(eia.ExtractionError, match='Non-retryable'):
        run(dataset, tmp_path, handler=handler, sleep=delays.append)
    assert len(calls) == 3  # Each route is attempted once; none is retried.
    assert delays == []


def test_retry_budget_and_retry_after(dataset, tmp_path):
    calls, delays = [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={'Retry-After': '3'})
    with pytest.raises(eia.ExtractionError, match='3 attempts'):
        run(dataset, tmp_path, handler=handler, attempts=3, sleep=delays.append)
    assert delays == [3, 3] * 3
    assert len(calls) == 9
    assert eia.retry_delay('999', 0, 1) == 30
    assert eia.retry_delay('garbage', 2, 1) == 4
    assert eia.retry_delay('NaN', 0, 1) == 1


@pytest.mark.parametrize('options', [{'page_size': 5001}, {'page_size': 0}, {'attempts': 0}, {'attempts': 7}, {'max_pages': 0}, {'backoff': float('nan')}, {'backoff': -1}])
def test_bad_configuration_fails_before_request(tmp_path, options):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('No request expected'))) as client:
        with pytest.raises(eia.ExtractionError):
            eia.extract(client, KEY, START, END, tmp_path, emit=lambda _: None, **options)


def test_structured_failure_logs_and_cli_credentials(dataset, tmp_path, monkeypatch, capsys):
    real_client = httpx.Client
    def factory(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda _: httpx.Response(403, text=KEY)))
    monkeypatch.setenv('EIA_API_KEY', KEY)
    monkeypatch.setattr(eia.httpx, 'Client', factory)
    assert eia.main(['--start', str(START), '--end', str(END), '--env-file', str(tmp_path / 'absent'), '--output-dir', str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert KEY not in captured.out + captured.err
    logs = [json.loads(line) for line in captured.err.splitlines()]
    assert [event['event'] for event in logs] == ['extraction_started', 'route_failed', 'route_failed', 'route_failed', 'extraction_failed', 'command_failed']
    assert logs[-2]['published'] is False
    assert logs[-2]['elapsed_seconds'] >= 0
    monkeypatch.delenv('EIA_API_KEY')
    assert eia.main(['--start', str(START), '--end', str(END), '--env-file', str(tmp_path / 'absent')]) == 1
    assert 'Missing EIA_API_KEY' in capsys.readouterr().err


def test_observed_overreported_facility_total_blocks_all_publication(dataset, tmp_path):
    run(dataset, tmp_path)
    before = current(tmp_path), fingerprint(tmp_path)
    events = []
    base = handler_for(dataset)
    def handler(request):
        response = base(request)
        if 'facility-nuclear-outages/data/' in request.url.path:
            payload = response.json()
            payload['response']['total'] = '95'
            return httpx.Response(200, json=payload)
        return response
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(eia.ExtractionError, match='Incomplete or inconsistent pagination'):
            eia.extract(client, KEY, START, END, tmp_path, page_size=2, emit=events.append)
    assert any(e['event'] == 'route_complete' and e['route'] == 'generator-nuclear-outages' for e in events)
    assert (current(tmp_path), fingerprint(tmp_path)) == before
    assert events[-1]['published'] is False


def test_native_json_decimals_are_not_rounded_before_validation():
    content = b'{"response":{"capacity":1.0000000000000001}}'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=content))) as client:
        body, _ = eia.request_body(client, eia.ROUTES[0] + '/', KEY, {}, attempts=1,
                                  backoff=1, sleep=lambda _: None, emit=lambda *a, **k: None)
    assert body['capacity'] == Decimal('1.0000000000000001')
    with pytest.raises(eia.ExtractionError, match='unrepresentable'):
        eia.numeric(body['capacity'], 'capacity')


def test_cli_success_and_interrupt_do_not_expose_credentials(dataset, tmp_path, monkeypatch, capsys):
    real_client = httpx.Client
    base = handler_for(dataset)
    monkeypatch.setenv('EIA_API_KEY', KEY)
    monkeypatch.setattr(eia.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(base)))
    args = ['--start', str(START), '--end', str(END), '--env-file', str(tmp_path / 'absent'), '--output-dir', str(tmp_path), '--page-size', '2']
    assert eia.main(args) == 0
    before = current(tmp_path), fingerprint(tmp_path)
    def interrupt(request):
        raise KeyboardInterrupt
    monkeypatch.setattr(eia.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(interrupt)))
    assert eia.main(args) == 130
    assert (current(tmp_path), fingerprint(tmp_path)) == before
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    assert all(isinstance(json.loads(line), dict) for line in output.err.splitlines())


def test_recorded_oct20_facility_total_95_with_55_rows_preserves_snapshot(tmp_path):
    """Replay independently recorded 55/0 pages, keeping total=95 unchanged."""
    fixtures = Path(__file__).parent / 'fixtures'
    recorded = json.loads((fixtures / 'eia_facility_pagination_2026-10-20.json').read_text())
    original_sample = json.loads((fixtures / 'eia_nuclear_sample.json').read_text())
    day = date(2026, 10, 20)
    pages = recorded['pages']
    assert [p['query']['offset'] for p in pages] == [0, 55]
    assert [len(p['response']['data']) for p in pages] == [55, 0]
    assert {p['response']['total'] for p in pages} == {'95'}
    rows = [row for p in pages for row in p['response']['data']]
    assert len({(row['period'], row['facility']) for row in rows}) == 55

    # A consistent synthetic source seeds a valid snapshot for the same window.
    # Its smaller totals are ONLY test setup, never an API correction policy.
    seed_rows = {
        route: [row for page in original_sample['routes'][route]['pages']
                for row in page['response']['data'] if row['period'] == str(day)]
        for route in eia.ROUTES
    }
    replay_fault = False
    offsets = []
    events = []

    def handler(request):
        route = request.url.path.removeprefix('/v2/nuclear-outages/').split('/')[0]
        if not request.url.path.endswith('/data/'):
            body = recorded['metadata'][route]
            version = pages[0]['apiVersion']
        else:
            offset = int(request.url.params['offset'])
            if replay_fault and route == 'facility-nuclear-outages':
                offsets.append(offset)
                page = next(p for p in pages if p['query']['offset'] == offset)
                assert dict(request.url.params) == {
                    **{k: str(v) for k, v in page['query'].items()}, 'api_key': KEY,
                }
                body, version = page['response'], page['apiVersion']
            else:
                values = seed_rows[route]
                body = {'frequency': 'daily', 'dateFormat': 'YYYY-MM-DD',
                        'total': str(len(values)), 'data': values[offset:offset + 100]}
                version = pages[0]['apiVersion']
        return httpx.Response(200, json={'response': body, 'apiVersion': version})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        eia.extract(client, KEY, day, day, tmp_path, page_size=100, emit=events.append)
        window = tmp_path / f'{day}_{day}'
        pointer_before = (window / 'current.json').read_bytes()
        run_dir = window / json.loads(pointer_before)['run']
        contents_before = {p.name: p.read_bytes() for p in run_dir.iterdir()}
        replay_fault = True
        events.clear()
        with pytest.raises(eia.ExtractionError, match='Incomplete or inconsistent pagination'):
            eia.extract(client, KEY, day, day, tmp_path, page_size=100, emit=events.append)
    assert offsets == [0, 55]
    assert [e['records'] for e in events if e['event'] == 'page_complete'
            and e['route'] == 'facility-nuclear-outages'] == [55]
    assert (window / 'current.json').read_bytes() == pointer_before
    assert {p.name: p.read_bytes() for p in run_dir.iterdir()} == contents_before
    assert not list(window.glob('.staging-*'))
    assert not list(window.glob('.current-*.json'))
    assert events[-1]['published'] is False
