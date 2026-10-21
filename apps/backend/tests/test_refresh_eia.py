"""Offline refresh tests: synthetic date windows and recorded EIA count faults."""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import fcntl
import json
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from outage_explorer import extract_eia as extractor
from outage_explorer import refresh_eia as refresh

KEY = 'fake-refresh-credential'
START = date(2026, 10, 18)
END = date(2026, 10, 20)


@pytest.fixture
def source():
    fixture = json.loads((Path(__file__).parent / 'fixtures/eia_nuclear_sample.json').read_text())
    result = {}
    for route in extractor.ROUTES:
        observed = fixture['routes'][route]
        templates = observed['pages'][0]['response']['data'][:2 if route == 'generator-nuclear-outages' else 1]
        result[route] = {'metadata': deepcopy(observed['metadata']), 'rows': []}
        for offset in range(7):
            day = (START + timedelta(days=offset)).isoformat()
            result[route]['rows'].extend({**row, 'period': day} for row in templates)
    return result


def transport(source, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(request)
        assert request.url.params['api_key'] == KEY
        route = request.url.path.removeprefix('/v2/nuclear-outages/').split('/')[0]
        if request.url.path.endswith('/data/'):
            params = request.url.params
            rows = [r for r in source[route]['rows'] if params['start'] <= r['period'] <= params['end']]
            columns = ['period', *extractor.IDENTIFIERS[route]]
            rows.sort(key=lambda r: tuple(r[c] for c in columns))
            offset, length = int(params['offset']), int(params['length'])
            body = {'frequency': 'daily', 'dateFormat': 'YYYY-MM-DD',
                    'total': str(len(rows)), 'data': rows[offset:offset + length]}
        else:
            body = source[route]['metadata']
        return httpx.Response(200, json={'response': body, 'apiVersion': '2.1.14',
                                       'request': {'params': {'api_key': KEY}}})
    return handler


def run(source, root, *, end=END, handler=None, calls=None, **options):
    with httpx.Client(transport=httpx.MockTransport(handler or transport(source, calls))) as client:
        return refresh.refresh(client, KEY, end, root, page_size=2, attempts=1,
                               sleep=lambda _: None, emit=lambda _: None, **options)


def snapshot(root):
    return refresh.load_snapshot(root)


def latest_audit(root):
    return max((json.loads(p.read_text()) for p in (root / 'ingestion-runs').glob('*.json')),
               key=lambda r: r['started_at_utc'])


def fingerprint(root):
    pointer = (root / 'current.json').read_bytes()
    directory = root / json.loads(pointer)['run']
    return pointer, {p.name: p.read_bytes() for p in directory.iterdir()}


def test_first_run_publishes_counts_watermarks_and_embedded_audit(source, tmp_path):
    manifest = run(source, tmp_path, start=START)
    assert manifest['start'] == str(START) and manifest['end'] == str(END)
    assert manifest['watermarks'] == {r: str(END) for r in extractor.ROUTES}
    assert [manifest['routes'][r]['records'] for r in extractor.ROUTES] == [3, 3, 6]
    audit = latest_audit(tmp_path)
    assert audit == manifest['ingestion_run']
    assert audit['status'] == 'published'
    assert audit['published_snapshot_version'] == snapshot(tmp_path)[2]
    assert audit['started_at_utc'] <= audit['finished_at_utc']
    for route, entry in audit['routes'].items():
        assert entry['status'] == 'published'
        assert entry['fetched_rows'] == entry['merged_rows'] == manifest['routes'][route]['records']
        assert entry['previous_rows'] == entry['unchanged'] == entry['replaced'] == 0


def test_missing_first_start_is_audited_without_network(source, tmp_path):
    calls = []
    with pytest.raises(extractor.ExtractionError, match='First refresh'):
        run(source, tmp_path, calls=calls)
    assert calls == []
    audit = latest_audit(tmp_path)
    assert audit['status'] == 'failed' and audit['failures']
    assert audit['published_snapshot_version'] is None
    assert not (tmp_path / 'current.json').exists()


def test_incremental_next_window_and_overlap_are_derived_from_current(source, tmp_path):
    run(source, tmp_path, start=START)
    calls = []
    new_end = END + timedelta(days=1)
    result = run(source, tmp_path, end=new_end, overlap_days=2, calls=calls)
    for request in calls:
        if request.url.path.endswith('/data/'):
            assert request.url.params['start'] == '2026-10-19'
            assert request.url.params['end'] == str(new_end)
    assert result['start'] == str(START)
    assert result['watermarks'] == {r: str(new_end) for r in extractor.ROUTES}
    assert [result['routes'][r]['records'] for r in extractor.ROUTES] == [4, 4, 8]
    audit = latest_audit(tmp_path)
    assert audit['requested_start'] == '2026-10-19'
    assert audit['requested_end'] == str(new_end)
    assert audit['routes']['us-nuclear-outages']['unchanged'] == 2
    assert audit['routes']['us-nuclear-outages']['inserted'] == 1
    assert audit['base_snapshot_version'] != audit['published_snapshot_version']


def test_zero_overlap_fetches_next_day_and_same_end_is_auditable_noop(source, tmp_path):
    run(source, tmp_path, start=START)
    calls = []
    run(source, tmp_path, end=END + timedelta(days=1), overlap_days=0, calls=calls)
    assert {r.url.params['start'] for r in calls if r.url.path.endswith('/data/')} == {'2026-10-21'}
    before = fingerprint(tmp_path)
    calls.clear()
    run(source, tmp_path, end=END + timedelta(days=1), overlap_days=0, calls=calls)
    assert calls == []
    assert fingerprint(tmp_path) == before
    audit = latest_audit(tmp_path)
    assert audit['status'] == 'no_op' and audit['published_snapshot_version'] is None


def test_overlap_clips_to_dataset_start_and_repeat_is_idempotent(source, tmp_path):
    run(source, tmp_path, start=START)
    before = snapshot(tmp_path)
    calls = []
    run(source, tmp_path, overlap_days=7, calls=calls)
    after = snapshot(tmp_path)
    assert before[1] == after[1]
    assert before[2] != after[2]  # Immutable versions, same logical rows.
    assert {r.url.params['start'] for r in calls if r.url.path.endswith('/data/')} == {str(START)}
    assert latest_audit(tmp_path)['routes']['generator-nuclear-outages']['unchanged'] == 6


def test_corrections_require_explicit_replace_policy(source, tmp_path):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    for row in source['facility-nuclear-outages']['rows']:
        if row['period'] == str(END):
            row['outage'] = '12.345'
    with pytest.raises(extractor.ExtractionError, match='failed validation'):
        run(source, tmp_path, overlap_days=1)
    assert fingerprint(tmp_path) == before
    failure = latest_audit(tmp_path)
    assert failure['status'] == 'failed'
    assert failure['routes']['facility-nuclear-outages']['status'] == 'failed'
    assert any('Conflicting overlap' in msg for msg in failure['failures'])
    result = run(source, tmp_path, overlap_days=1, conflict_policy='replace')
    _, rows, _ = snapshot(tmp_path)
    assert rows['facility-nuclear-outages'][(END, '46')]['outage'] == Decimal('12.345000')
    assert len(rows['facility-nuclear-outages']) == 3
    assert result['ingestion_run']['routes']['facility-nuclear-outages']['replaced'] == 1


def test_new_keys_insert_but_missing_old_keys_and_source_duplicates_fail(source, tmp_path):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    route = 'generator-nuclear-outages'
    original = deepcopy(source[route]['rows'])
    source[route]['rows'] = [r for r in original if not (r['period'] == str(END) and r['generator'] == '2')]
    with pytest.raises(extractor.ExtractionError):
        run(source, tmp_path, overlap_days=1, conflict_policy='replace')
    assert fingerprint(tmp_path) == before
    assert any('omits previously published' in msg for msg in latest_audit(tmp_path)['failures'])
    source[route]['rows'] = original + [deepcopy(original[0])]
    with pytest.raises(extractor.ExtractionError):
        run(source, tmp_path, conflict_policy='replace')
    assert fingerprint(tmp_path) == before
    assert any('Duplicate' in msg for msg in latest_audit(tmp_path)['failures'])
    source[route]['rows'] = original
    extra = deepcopy(original[0]); extra['generator'] = '001A'; extra['period'] = str(END)
    source[route]['rows'].append(extra)
    run(source, tmp_path, overlap_days=1)
    assert len(snapshot(tmp_path)[1][route]) == 7


@pytest.mark.parametrize('fault', ['parquet_schema', 'watermark', 'snapshot_version', 'stored_duplicate', 'source_schema'])
def test_incompatible_or_inconsistent_state_cannot_advance(source, tmp_path, fault):
    run(source, tmp_path, start=START)
    directory = tmp_path / json.loads((tmp_path / 'current.json').read_text())['run']
    file = directory / 'us-nuclear-outages.parquet'
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if fault == 'parquet_schema':
        table = pq.read_table(file)
        i = table.schema.get_field_index('capacity')
        table = table.set_column(i, 'capacity', pa.array([float(v.as_py()) for v in table.column(i)]))
        pq.write_table(table, file)
    elif fault == 'stored_duplicate':
        table = pq.read_table(file)
        pq.write_table(pa.concat_tables([table, table.slice(0, 1)]), file)
    elif fault == 'source_schema':
        source['facility-nuclear-outages']['rows'][0]['new_field'] = 'unreviewed'
    else:
        if fault == 'watermark':
            manifest['watermarks']['us-nuclear-outages'] = str(END - timedelta(days=1))
        else:
            manifest['snapshot_version'] = 'bad'
        manifest_path.write_text(json.dumps(manifest))
    before = fingerprint(tmp_path)
    calls = []
    with pytest.raises(extractor.ExtractionError):
        run(source, tmp_path, calls=calls)
    assert fingerprint(tmp_path) == before
    assert latest_audit(tmp_path)['status'] == 'failed'
    assert latest_audit(tmp_path)['published_snapshot_version'] is None
    if fault != 'source_schema':
        assert calls == []


def test_recorded_facility_discrepancy_still_blocks_refresh_and_watermarks(source, tmp_path):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    recording = json.loads((Path(__file__).parent / 'fixtures/eia_facility_pagination_2026-10-20.json').read_text())
    base = transport(source)
    def handler(request):
        if 'facility-nuclear-outages/data/' in request.url.path:
            offset = int(request.url.params['offset'])
            # Recorded response shape is unchanged; the test uses its length 100.
            page = next(p for p in recording['pages'] if p['query']['offset'] == offset)
            return httpx.Response(200, json={'response': page['response'], 'apiVersion': page['apiVersion']})
        return base(request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(extractor.ExtractionError):
            refresh.refresh(client, KEY, END, tmp_path, overlap_days=1, page_size=100,
                            attempts=1, emit=lambda _: None)
    assert fingerprint(tmp_path) == before
    audit = latest_audit(tmp_path)
    assert audit['routes']['facility-nuclear-outages']['fetched_rows'] == 55
    assert any('Incomplete or inconsistent pagination' in f for f in audit['failures'])
    assert snapshot(tmp_path)[0]['watermarks'] == {r: str(END) for r in extractor.ROUTES}


@pytest.mark.parametrize('fault', ['interrupt', 'disk_before_publication', 'http'])
def test_failed_runs_preserve_snapshot_and_leave_audit(source, tmp_path, monkeypatch, fault):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    base = transport(source)
    def handler(request):
        if 'generator-nuclear-outages/data/' in request.url.path:
            if fault == 'interrupt':
                raise KeyboardInterrupt
            if fault == 'http':
                return httpx.Response(503, text=KEY)
        return base(request)
    if fault == 'disk_before_publication':
        replace = refresh.os.replace
        def fail_pointer(source, target):
            if Path(target).name == 'current.json':
                raise OSError(KEY)
            return replace(source, target)
        monkeypatch.setattr(refresh.os, 'replace', fail_pointer)
    exception = KeyboardInterrupt if fault == 'interrupt' else OSError if fault == 'disk_before_publication' else extractor.ExtractionError
    with pytest.raises(exception):
        run(source, tmp_path, handler=handler, end=END + timedelta(days=1))
    assert fingerprint(tmp_path) == before
    audit = latest_audit(tmp_path)
    assert audit['status'] == ('interrupted' if fault == 'interrupt' else 'failed')
    assert audit['finished_at_utc'] and audit['failures']
    assert audit['published_snapshot_version'] is None
    assert all(e['status'] != 'published' for e in audit['routes'].values())
    assert KEY not in json.dumps(audit)
    assert not list(tmp_path.glob('.staging-*'))


def test_adopt_existing_window_snapshot_explicitly(source, tmp_path):
    legacy = tmp_path / 'legacy'
    with httpx.Client(transport=httpx.MockTransport(transport(source))) as client:
        extractor.extract(client, KEY, START, END, legacy, emit=lambda _: None)
    legacy_window = legacy / f'{START}_{END}'
    legacy_before = fingerprint(legacy_window)
    target = tmp_path / 'incremental'
    result = run(source, target, end=END + timedelta(days=1), overlap_days=1, base_snapshot=legacy_window)
    assert result['start'] == str(START)
    assert result['end'] == '2026-10-21'
    assert result['routes']['us-nuclear-outages']['records'] == 4
    assert fingerprint(legacy_window) == legacy_before


def test_writer_lock_rejects_concurrent_refresh_and_audits_it(source, tmp_path):
    calls = []
    with (tmp_path / '.refresh.lock').open('a') as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(extractor.ExtractionError, match='writer lock'):
            run(source, tmp_path, start=START, calls=calls)
    assert calls == []
    assert latest_audit(tmp_path)['status'] == 'failed'


def test_stale_unpublished_run_is_audited_as_incomplete(source, tmp_path):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    stale = deepcopy(latest_audit(tmp_path))
    stale.update(run_id='a' * 32, status='running', finished_at_utc=None, published_snapshot_version=None)
    stale_path = tmp_path / 'ingestion-runs' / f'{stale["run_id"]}.json'
    stale_path.write_text(json.dumps(stale))
    run(source, tmp_path, overlap_days=0)
    assert json.loads(stale_path.read_text())['status'] == 'incomplete'
    assert fingerprint(tmp_path) == before


def test_published_manifest_recovers_audit_write_gap(source, tmp_path, monkeypatch):
    original = refresh.atomic_json
    def fail_success_audit(path, value):
        if value.get('status') == 'published' and path.parent.name == 'ingestion-runs':
            raise OSError(KEY)
        return original(path, value)
    monkeypatch.setattr(refresh, 'atomic_json', fail_success_audit)
    result = run(source, tmp_path, start=START)
    assert snapshot(tmp_path)[2] == result['snapshot_version']
    assert latest_audit(tmp_path)['status'] == 'running'
    monkeypatch.setattr(refresh, 'atomic_json', original)
    version = result['snapshot_version']
    run(source, tmp_path, overlap_days=0)
    recovered = json.loads((tmp_path / 'ingestion-runs' / f'{version}.json').read_text())
    assert recovered == result['ingestion_run']
    assert recovered['status'] == 'published'


@pytest.mark.parametrize('options', [{'overlap_days': -1}, {'conflict_policy': 'ignore'}, {'end': END - timedelta(days=1)}, {'start': START}])
def test_invalid_refresh_parameters_do_not_network_or_rewind(source, tmp_path, options):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    calls = []
    with pytest.raises(extractor.ExtractionError):
        run(source, tmp_path, calls=calls, **options)
    assert calls == [] and fingerprint(tmp_path) == before
    assert latest_audit(tmp_path)['status'] == 'failed'


def test_cli_offline_success_and_missing_credential_audit(source, tmp_path, monkeypatch, capsys):
    real_client = httpx.Client
    calls = []
    monkeypatch.setattr(refresh.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(transport(source, calls))))
    monkeypatch.setenv('EIA_API_KEY', KEY)
    args = ['--start', str(START), '--end', str(END), '--env-file', str(tmp_path / 'absent'), '--output-dir', str(tmp_path / 'ok')]
    assert refresh.main(args) == 0
    assert latest_audit(tmp_path / 'ok')['status'] == 'published'
    monkeypatch.delenv('EIA_API_KEY')
    calls.clear()
    args[-1] = str(tmp_path / 'missing-key')
    assert refresh.main(args) == 1
    assert calls == []
    assert latest_audit(tmp_path / 'missing-key')['status'] == 'failed'
    output = capsys.readouterr()
    assert KEY not in output.out + output.err
    for line in output.err.splitlines():
        json.loads(line)


def test_schema_metadata_change_and_failed_watermark_audit(source, tmp_path):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    source['facility-nuclear-outages']['metadata']['facets'].append({'id': 'unknown'})
    with pytest.raises(extractor.ExtractionError):
        run(source, tmp_path, end=END + timedelta(days=1))
    assert fingerprint(tmp_path) == before
    audit = latest_audit(tmp_path)
    for entry in audit['routes'].values():
        assert entry['watermark_before'] == entry['watermark_after'] == str(END)
    assert any('dimensions/measures changed' in text for text in audit['failures'])


def test_all_route_watermarks_match_stored_maxima_after_merge(source, tmp_path):
    run(source, tmp_path, start=START)
    result = run(source, tmp_path, end=END + timedelta(days=2), overlap_days=1)
    manifest, rows, version = snapshot(tmp_path)
    assert result['snapshot_version'] == version
    for route in extractor.ROUTES:
        actual = max(k[0] for k in rows[route]).isoformat()
        assert manifest['watermarks'][route] == actual == '2026-10-22'
        assert result['ingestion_run']['routes'][route]['watermark_before'] == str(END)
        assert result['ingestion_run']['routes'][route]['watermark_after'] == actual


@pytest.mark.parametrize('mirror_failure', [False, True])
def test_interrupt_immediately_after_pointer_replace_is_published(source, tmp_path, monkeypatch, mirror_failure):
    run(source, tmp_path, start=START)
    replace = refresh.os.replace
    atomic = refresh.atomic_json
    def write_audit(path, value):
        if mirror_failure and value.get('status') == 'published':
            raise OSError('simulated mirror failure')
        return atomic(path, value)
    monkeypatch.setattr(refresh, 'atomic_json', write_audit)
    def interrupt_after_replace(src, dst):
        replace(src, dst)
        if Path(dst).name == 'current.json':
            raise KeyboardInterrupt
    monkeypatch.setattr(refresh.os, 'replace', interrupt_after_replace)
    with pytest.raises(KeyboardInterrupt):
        run(source, tmp_path, end=END + timedelta(days=1))
    manifest, _, version = snapshot(tmp_path)
    audit = latest_audit(tmp_path)
    if mirror_failure:
        assert audit['status'] == 'running'
    else:
        assert audit == manifest['ingestion_run']
        assert audit['status'] == 'published'
        assert audit['published_snapshot_version'] == version
    assert manifest['watermarks'] == {r: '2026-10-21' for r in extractor.ROUTES}
    assert all(e['watermark_after'] == '2026-10-21' for e in manifest['ingestion_run']['routes'].values())
    monkeypatch.setattr(refresh.os, 'replace', replace)
    monkeypatch.setattr(refresh, 'atomic_json', atomic)
    run(source, tmp_path, end=END + timedelta(days=1), overlap_days=0)
    assert snapshot(tmp_path)[2] == version
    assert json.loads((tmp_path / 'ingestion-runs' / f'{version}.json').read_text()) == manifest['ingestion_run']


@pytest.mark.parametrize('status', ['interrupted', 'failed', 'published', 'missing', 'malformed'])
def test_current_audit_is_restored_from_validated_snapshot(source, tmp_path, status):
    manifest = run(source, tmp_path, start=START)
    version = manifest['snapshot_version']
    path = tmp_path / 'ingestion-runs' / f'{version}.json'
    if status == 'missing':
        path.unlink()
    elif status == 'malformed':
        path.write_bytes(b'{broken-json')
    else:
        bad = deepcopy(manifest['ingestion_run'])
        bad.update(status=status, published_snapshot_version=None)
        for entry in bad['routes'].values():
            entry['watermark_after'] = None
        path.write_text(json.dumps(bad))
    evidence = path.read_bytes() if path.exists() else None
    calls = []
    run(source, tmp_path, overlap_days=0, calls=calls)
    assert calls == []
    assert json.loads(path.read_text()) == manifest['ingestion_run']
    preserved = list(path.parent.glob(f'{version}.json.*.corrupt'))
    assert len(preserved) == (evidence is not None)
    if evidence is not None:
        assert preserved[0].read_bytes() == evidence
    assert snapshot(tmp_path)[2] == version


@pytest.mark.parametrize('contents', [b'{broken', b'[]', b'\xff', b'{"status":"running","failures":42}', b'{"status":[]}'])
def test_unrelated_corrupt_audit_preserves_evidence_without_blocking(source, tmp_path, contents):
    run(source, tmp_path, start=START)
    path = tmp_path / 'ingestion-runs' / ('a' * 32 + '.json')
    path.write_bytes(contents)
    result = run(source, tmp_path, end=END + timedelta(days=1))
    assert result['end'] == '2026-10-21'
    assert not path.exists()
    evidence = list(path.parent.glob(f'{path.name}.*.corrupt'))
    assert len(evidence) == 1 and evidence[0].read_bytes() == contents
    run(source, tmp_path, end=END + timedelta(days=1), overlap_days=0)
    assert len(list(path.parent.glob('*.corrupt'))) == 1


@pytest.mark.parametrize('fault', ['pointer', 'manifest', 'embedded_watermark', 'embedded_count'])
def test_current_publication_corruption_is_not_quarantined_or_hidden(source, tmp_path, fault):
    manifest = run(source, tmp_path, start=START)
    pointer = tmp_path / 'current.json'
    path = tmp_path / json.loads(pointer.read_text())['run'] / 'manifest.json'
    if fault == 'pointer':
        pointer.write_text('{broken')
    elif fault == 'manifest':
        path.write_text('{broken')
    else:
        entry = manifest['ingestion_run']['routes']['us-nuclear-outages']
        entry['watermark_after' if fault == 'embedded_watermark' else 'merged_rows'] = None
        path.write_text(json.dumps(manifest))
    before = pointer.read_bytes(), path.read_bytes()
    calls = []
    with pytest.raises(extractor.ExtractionError):
        run(source, tmp_path, calls=calls)
    assert calls == []
    assert (pointer.read_bytes(), path.read_bytes()) == before
    assert not list((tmp_path / 'ingestion-runs').glob('*.corrupt'))


def test_hive_style_ancestors_do_not_change_physical_schema(source, tmp_path):
    root = tmp_path / 'source=eia' / 'period=arbitrary' / 'incremental'
    run(source, root, start=START)
    before = snapshot(root)
    run(source, root)
    assert snapshot(root)[1] == before[1]
    run(source, root, end=END + timedelta(days=1))
    manifest, rows, _ = snapshot(root)
    assert manifest['end'] == '2026-10-21'
    assert all('source' not in row for route in rows.values() for row in route.values())


def test_new_directory_links_and_audit_are_synced_before_network(source, tmp_path, monkeypatch):
    root = tmp_path / 'new' / 'nested' / 'output'
    synced = []
    original = extractor.sync_directory
    def sync(path):
        original(path)
        synced.append(path)
    monkeypatch.setattr(extractor, 'sync_directory', sync)
    def handler(request):
        assert {tmp_path, tmp_path / 'new', root.parent, root, root / 'ingestion-runs'} <= set(synced)
        assert latest_audit(root)['status'] == 'running'
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        run(source, root, start=START, handler=handler)
    assert latest_audit(root)['status'] == 'interrupted'


def test_directory_sync_failure_prevents_network(source, tmp_path, monkeypatch):
    calls = []
    def fail(path):
        raise OSError('simulated directory sync failure')
    monkeypatch.setattr(extractor, 'sync_directory', fail)
    with pytest.raises(OSError):
        run(source, tmp_path / 'new-root', start=START, calls=calls)
    assert calls == []


def test_evidence_preservation_failure_aborts_without_losing_corruption(source, tmp_path, monkeypatch):
    run(source, tmp_path, start=START)
    before = fingerprint(tmp_path)
    path = tmp_path / 'ingestion-runs' / ('b' * 32 + '.json')
    path.write_bytes(b'{broken')
    replace = refresh.os.replace
    def fail_quarantine(src, dst):
        if Path(dst).suffix == '.corrupt':
            raise OSError('simulated evidence preservation failure')
        return replace(src, dst)
    monkeypatch.setattr(refresh.os, 'replace', fail_quarantine)
    calls = []
    with pytest.raises(OSError):
        run(source, tmp_path, calls=calls)
    assert calls == [] and path.read_bytes() == b'{broken'
    assert fingerprint(tmp_path) == before
