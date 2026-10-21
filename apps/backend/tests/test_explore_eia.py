"""Offline tests. Synthetic values exercise edge cases, not EIA observations."""

import json
from pathlib import Path

import httpx
import pytest

from outage_explorer import explore_eia as eia


@pytest.fixture
def metadata():
    return {
        "frequency": [{"id": "daily"}],
        "facets": [{"id": "facility"}, {"id": "generator"}],
        "data": {"capacity": {"units": "megawatts"}, "outage": {"units": "megawatts"}, "percentOutage": {"units": "percent"}},
        "endPeriod": "2026-10-20",
    }


def test_load_key_from_file_without_overriding_environment(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    path.write_text('EIA_API_KEY="fake-file-key"\n')
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    assert eia.load_api_key(path) == "fake-file-key"
    monkeypatch.setenv("EIA_API_KEY", "fake-exported-key")
    assert eia.load_api_key(path) == "fake-exported-key"
    assert capsys.readouterr() == ("", "")


def test_missing_key(tmp_path, monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with pytest.raises(eia.ExplorationError, match="Missing EIA_API_KEY"):
        eia.load_api_key(tmp_path / "absent.env")


def test_all_routes_bounded_pages_and_discovered_measures(metadata):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["api_key"] == "fake-key"
        if not request.url.path.endswith("/data/"):
            return httpx.Response(200, json={"response": metadata, "request": {"params": {"api_key": "fake-key"}}, "apiVersion": "test"})
        query = request.url.params
        assert query["length"] == "2"
        assert query["end"] == metadata["endPeriod"]
        assert [query[f"data[{i}]"] for i in range(3)] == list(metadata["data"])
        assert [query[f"sort[{i}][column]"] for i in range(3)] == ["period", "facility", "generator"]
        offset = int(query["offset"])
        return httpx.Response(200, json={"response": {"total": "100", "data": [{"period": "2026-10-20", "facility": "001", "generator": str(offset + i), "capacity": "100"} for i in range(2)]}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = eia.explore(client, "fake-key", length=2)
    assert len(requests) == 9
    assert set(report["routes"]) == set(eia.ROUTES)
    for route in report["routes"].values():
        assert [p["query"]["offset"] for p in route["pages"]] == [0, 2]
        assert route["inspection"]["row_count"] == 4
        assert route["inspection"]["candidate_key"]["duplicate_rows"] == 0
    assert "fake-key" not in eia.safe_json(report, "fake-key")
    assert '"request"' not in eia.safe_json(report, "fake-key")


@pytest.mark.parametrize("length", [0, 11, -1])
def test_reject_unbounded_samples(length):
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Must not request"))) as client:
        with pytest.raises(eia.ExplorationError, match="between 1 and 10"):
            eia.explore(client, "fake-key", length)


def test_field_nulls_missing_dates_and_candidate_duplicates():
    rows = [
        {"period": "2026-10-20", "facility": "001", "capacity": "100.5", "outage": None},
        {"period": "2026-10-20", "facility": "001", "capacity": "", "outage": "0"},
        {"period": "2026-02-30", "facility": None, "capacity": "not-numeric"},
        {"facility": "002", "capacity": 12},
    ]
    result = eia.inspect_rows(rows, ["period", "facility"])
    assert result["fields"]["outage"]["null"] == 1
    assert result["fields"]["outage"]["missing"] == 2
    assert result["fields"]["capacity"]["empty_strings"] == 1
    assert result["fields"]["capacity"]["numeric_parseable"] == 2
    assert result["fields"]["facility"]["examples"][0] == "001"
    assert result["period"]["invalid_or_missing"] == 2
    assert result["candidate_key"]["duplicate_rows"] == 1
    assert result["candidate_key"]["incomplete_rows"] == 2
    assert eia.inspect_rows([], ["period"])["row_count"] == 0


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_http_errors_do_not_expose_key_or_error_body(status):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status, text="fake-secret"))) as client:
        with pytest.raises(eia.ExplorationError, match=f"HTTP {status}") as exc:
            eia.fetch_response(client, "us-nuclear-outages/", "fake-secret")
    assert "fake-secret" not in str(exc.value)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not JSON"),
    httpx.Response(200, json=[]),
    httpx.Response(200, json={"error": "fake-secret", "code": 400}),
    httpx.Response(200, json={"response": []}),
    httpx.Response(200, json={}),
])
def test_malformed_envelopes(response):
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(eia.ExplorationError, match="Malformed") as exc:
            eia.fetch_response(client, "us-nuclear-outages/", "fake-secret")
    assert "fake-secret" not in str(exc.value)


@pytest.mark.parametrize("body", [{}, {"total": "1", "data": [None]}, {"total": "bad", "data": []}, {"total": True, "data": []}, {"total": -1, "data": []}, {"total": 0, "data": [{}]}, {"total": 2, "data": [{}, {}]}])
def test_malformed_or_oversized_data_page(body):
    with pytest.raises(eia.ExplorationError):
        eia.read_page(body, 1)


@pytest.mark.parametrize("field,value", [("data", []), ("data", {}), ("facets", [None]), ("frequency", [{"id": "monthly"}])])
def test_malformed_metadata(metadata, field, value):
    metadata[field] = value
    with pytest.raises(eia.ExplorationError):
        eia.metadata_fields(metadata)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_failure_continues_other_routes(error):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        raise error("https://example.invalid/?api_key=fake-secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = eia.explore(client, "fake-secret")
    assert count == 3
    assert all("Network failure" in r["error"] for r in report["routes"].values())
    assert "fake-secret" not in json.dumps(report)


def test_stops_at_empty_page(metadata):
    def handler(request):
        body = {"data": [], "total": "0"} if request.url.path.endswith("/data/") else metadata
        return httpx.Response(200, json={"response": body})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = eia.explore_route(client, eia.ROUTES[0], "fake-key", 3)
    assert len(result["pages"]) == 1
    assert not result["pages"][0]["has_more"]


def test_redacts_nested_echo_and_key_in_arbitrary_values():
    report = {"request": {"params": {"api_key": "fake-secret"}}, "nested": {"api_key": "fake-secret", "warning": "url?api_key=fake-secret"}}
    rendered = eia.safe_json(report, "fake-secret")
    assert "fake-secret" not in rendered
    assert json.loads(rendered) == {"nested": {"warning": "url?api_key=[REDACTED]"}}


def test_cli_missing_key_is_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    assert eia.main(["--env-file", str(tmp_path / "absent")]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Missing EIA_API_KEY" in captured.err


def test_cli_errors_return_nonzero_and_sanitize_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EIA_API_KEY", "fake-secret")
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403, text="fake-secret")))
    monkeypatch.setattr(eia.httpx, "Client", lambda **_: client)
    assert eia.main(["--env-file", str(tmp_path / "absent")]) == 1
    captured = capsys.readouterr()
    assert "fake-secret" not in captured.out + captured.err
    assert all("HTTP 403" in r["error"] for r in json.loads(captured.out)["routes"].values())


def test_partial_result_retains_metadata_when_data_fails(metadata):
    def handler(request):
        if request.url.path.endswith('/data/'):
            return httpx.Response(503, text='fake-secret')
        return httpx.Response(200, json={'response': metadata})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = eia.explore(client, 'fake-secret')
    for route in report['routes'].values():
        assert route['metadata'] == metadata
        assert route['pages'] == []
        assert 'Data page at offset 0: HTTP 503' in route['error']
    assert 'fake-secret' not in json.dumps(report)


def test_nonstandard_json_is_rejected():
    response = httpx.Response(200, content=b'{"response":{"data":[{"capacity":NaN}]}}')
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(eia.ExplorationError, match='Malformed response'):
            eia.fetch_response(client, eia.ROUTES[0], 'fake-key')


def test_invalid_metadata_date_is_rejected(metadata):
    metadata['endPeriod'] = 'not-a-date'
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'response': metadata}))) as client:
        with pytest.raises(eia.ExplorationError, match='endPeriod'):
            eia.explore_route(client, eia.ROUTES[0], 'fake-key', 3)


def test_dns_error_is_identified_without_exception_url():
    import socket

    def handler(request):
        try:
            raise socket.gaierror('fake-secret')
        except socket.gaierror as exc:
            raise httpx.ConnectError('fake-secret', request=request) from exc

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(eia.ExplorationError, match='DNS resolution failed') as exc:
            eia.fetch_response(client, eia.ROUTES[0], 'fake-secret')
    assert 'fake-secret' not in str(exc.value)


def test_recorded_real_responses_replay_all_routes():
    fixture = json.loads((Path(__file__).parent / 'fixtures/eia_nuclear_sample.json').read_text())
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path.removeprefix('/v2/nuclear-outages/').strip('/').split('/')
        recorded = fixture['routes'][path[0]]
        if len(path) == 1:
            body = recorded['metadata']
        else:
            offset = int(request.url.params['offset'])
            page = next(p for p in recorded['pages'] if p['query']['offset'] == offset)
            assert dict(request.url.params) == {'api_key': 'fake-replay-key', **{k: str(v) for k, v in page['query'].items()}}
            body = page['response']
        return httpx.Response(200, json={'response': body, 'apiVersion': recorded['apiVersion']})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = eia.explore(client, 'fake-replay-key')
    assert len(calls) == 9
    shared = {'period', 'capacity', 'outage', 'percentOutage', 'capacity-units', 'outage-units', 'percentOutage-units'}
    for name, route in report['routes'].items():
        assert 'error' not in route
        inspection = route['inspection']
        assert inspection['row_count'] == 6
        assert inspection['candidate_key']['duplicate_rows'] == 0
        assert inspection['candidate_key']['incomplete_rows'] == 0
        assert inspection['period']['invalid_or_missing'] == 0
        expected = shared | ({'facility', 'facilityName'} if name != 'us-nuclear-outages' else set())
        if name == 'generator-nuclear-outages':
            expected |= {'generator'}
        assert set(inspection['fields']) == expected
        for field in inspection['fields'].values():
            assert field['json_types'] == ['str']
            assert field['null'] == field['missing'] == field['empty_strings'] == 0
        for measure in ['capacity', 'outage', 'percentOutage']:
            assert inspection['fields'][measure]['numeric_parseable'] == 6
    generators = [row for page in report['routes']['generator-nuclear-outages']['pages'] for row in page['response']['data']]
    # The real sample demonstrates why generator alone cannot identify a unit.
    assert len({row['generator'] for row in generators}) < len(generators)
    assert len({(row['facility'], row['generator']) for row in generators}) == len(generators)


def test_cli_writes_sanitized_report_and_preserves_env_files(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / '.env'
    original = 'EIA_API_KEY=fake-secret\n'
    env_file.write_text(original)
    monkeypatch.setenv('EIA_API_KEY', 'fake-secret')
    real_client = httpx.Client

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda _: httpx.Response(403)))

    monkeypatch.setattr(eia.httpx, 'Client', client_factory)
    output = tmp_path / 'data/report.json'
    assert eia.main(['--env-file', str(env_file), '--output', str(output)]) == 1
    assert 'fake-secret' not in output.read_text()
    assert len(json.loads(output.read_text())['routes']) == 3
    assert eia.main(['--env-file', str(env_file), '--output', str(env_file)]) == 1
    assert env_file.read_text() == original
    alias = tmp_path / 'report.json'
    alias.symlink_to(env_file)
    assert eia.main(['--env-file', str(env_file), '--output', str(alias)]) == 1
    assert env_file.read_text() == original
    captured = capsys.readouterr()
    assert 'fake-secret' not in captured.out + captured.err
