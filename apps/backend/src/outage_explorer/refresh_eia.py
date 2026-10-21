"""Local cumulative refreshes built on the strict EIA date-window extractor."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from uuid import uuid4

import httpx
import pyarrow as pa
import pyarrow.parquet as pq

from outage_explorer import extract_eia as extraction
from outage_explorer.explore_eia import DEFAULT_ENV, ROUTES, ExplorationError, load_api_key, safe_json

Error = extraction.ExtractionError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ValueError, OSError):
        raise Error("Missing or malformed snapshot/audit metadata.") from None


def durable_directory(path: Path) -> None:
    """Persist directory links before relying on files beneath them (local POSIX)."""
    if not path.parent.exists():
        durable_directory(path.parent)
    path.mkdir(exist_ok=True)
    extraction.sync_directory(path.parent)


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        extraction.write_json(temporary, value)
        os.replace(temporary, path)
        extraction.sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def validated_rows(path: Path, route: str, start: date, end: date) -> dict[tuple, dict]:
    expected = extraction.schema_for(route)
    if not pq.ParquetFile(path).schema_arrow.equals(expected, check_metadata=True):
        raise Error("Incompatible Parquet schema; automatic migration is not supported.")
    result = {}
    periods = set()
    for row in pq.ParquetFile(path).read().to_pylist():
        period = row.get("period")
        if not isinstance(period, date):
            raise Error("Invalid stored date.")
        wire = {**row, "period": period.isoformat()}
        record, key = extraction.validate_record(wire, route, start, end)
        if key in result:
            raise Error("Duplicate candidate key in stored data.")
        result[key] = record
        periods.add(period)
    if not result or len(periods) != (end - start).days + 1:
        raise Error("Stored daily coverage is incomplete.")
    return result


def load_snapshot(root: Path) -> tuple[dict, dict, str]:
    pointer = read_json(root / "current.json")
    relative = pointer.get("run")
    if not isinstance(relative, str) or not re.fullmatch(r"runs/[a-f0-9]{32}", relative):
        raise Error("Invalid snapshot pointer.")
    directory = (root / relative).resolve()
    if not directory.is_relative_to(root.resolve()):
        raise Error("Snapshot path escapes its root.")
    manifest = read_json(directory / "manifest.json")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in (1, 2):
        raise Error("Unsupported snapshot schema version.")
    start = extraction.parse_date(manifest.get("start"))
    end = extraction.parse_date(manifest.get("end"))
    if start > end or not isinstance(manifest.get("routes"), dict) or set(manifest["routes"]) != set(ROUTES):
        raise Error("Invalid snapshot coverage/routes.")
    rows = {}
    watermarks = {}
    for route in ROUTES:
        entry = manifest["routes"][route]
        if not isinstance(entry, dict) or entry.get("file") != f"{route}.parquet":
            raise Error("Invalid route manifest.")
        path = directory / entry["file"]
        if not path.resolve().is_relative_to(directory):
            raise Error("Route file escapes snapshot directory.")
        rows[route] = validated_rows(path, route, start, end)
        if type(entry.get("records")) is not int or entry["records"] != len(rows[route]):
            raise Error("Stored row count does not match its manifest.")
        if entry.get("candidate_key") != ["period", *extraction.IDENTIFIERS[route]]:
            raise Error("Stored candidate key definition changed.")
        watermarks[route] = max(key[0] for key in rows[route]).isoformat()
    if set(watermarks.values()) != {end.isoformat()}:
        raise Error("Route watermarks are inconsistent with daily coverage.")
    if manifest["schema_version"] == 2:
        if manifest.get("watermarks") != watermarks or manifest.get("snapshot_version") != directory.name:
            raise Error("Stored watermarks or snapshot version are inconsistent.")
        embedded = manifest.get("ingestion_run", {})
        if (not isinstance(embedded, dict) or embedded.get("run_id") != directory.name
                or embedded.get("status") != "published"
                or embedded.get("published_snapshot_version") != directory.name):
            raise Error("Snapshot ingestion metadata is inconsistent.")
        entries = embedded.get("routes")
        if (embedded.get("requested_end") != end.isoformat()
                or not embedded.get("finished_at_utc") or embedded.get("failures") != []
                or not isinstance(entries, dict) or set(entries) != set(ROUTES)):
            raise Error("Snapshot ingestion metadata is inconsistent.")
        for route, entry in entries.items():
            if (not isinstance(entry, dict) or entry.get("status") != "published"
                    or entry.get("watermark_after") != watermarks[route]
                    or type(entry.get("merged_rows")) is not int
                    or entry["merged_rows"] != len(rows[route])):
                raise Error("Snapshot ingestion route metadata is inconsistent.")
    # A legacy window snapshot has no stored watermarks; these are derived only
    # after validating its actual rows, coverage, counts, and schema.
    manifest = {**manifest, "watermarks": watermarks}
    return manifest, rows, directory.name


def merge_rows(old: dict, incoming: dict, start: date, end: date, policy: str) -> tuple[dict, dict]:
    missing = {key for key in old if start <= key[0] <= end} - incoming.keys()
    if missing:
        raise Error("Refresh omits previously published keys; deletions require investigation.")
    result = dict(old)
    counts = {"inserted": 0, "unchanged": 0, "replaced": 0}
    for key, row in incoming.items():
        if key not in old:
            counts["inserted"] += 1
        elif row == old[key]:
            counts["unchanged"] += 1
        elif policy == "replace":
            counts["replaced"] += 1
        else:
            raise Error("Conflicting overlap record; explicitly select the replace policy to accept corrections.")
        result[key] = row
    return result, counts


def reconcile_audits(audit_dir: Path, current: dict | None, version: str | None,
                     run_id: str, report=None) -> None:
    """Recover mirrors only after the authoritative snapshot has been validated."""
    authoritative = (current["ingestion_run"]
                     if current and current.get("schema_version") == 2 else None)
    for path in list(audit_dir.glob("*.json")):
        if path.stem == run_id:
            continue
        try:
            record = read_json(path)
            if (record.get("run_id") != path.stem
                    or record.get("status") not in ("running", "published", "failed", "interrupted", "incomplete", "no_op")
                    or not isinstance(record.get("failures"), list)
                    or not all(isinstance(reason, str) for reason in record["failures"])):
                raise Error("Malformed historical audit.")
        except Error:
            record = None
        committed = authoritative if path.stem == version else None
        if record is None or (committed is not None and record != committed):
            # Keep the original bytes, including valid-but-contradictory mirrors.
            evidence = path.with_name(f"{path.name}.{uuid4().hex}.corrupt")
            os.replace(path, evidence)
            extraction.sync_directory(audit_dir)
            if report:
                report("audit_evidence_preserved", evidence=evidence.name,
                       recovered_publication=committed is not None)
            if committed is not None:
                atomic_json(path, committed)
            continue
        if record.get("status") == "running":
            record.update(status="incomplete", finished_at_utc=now(),
                          failures=[*record.get("failures", []),
                                    "Previous writer ended without a committed snapshot."],
                          published_snapshot_version=None)
            atomic_json(path, record)
    if authoritative is not None and not (audit_dir / f"{version}.json").exists():
        atomic_json(audit_dir / f"{version}.json", authoritative)


def refresh(
    client: httpx.Client, key: str, end: date, output_dir: Path,
    *, start: date | None = None, overlap_days: int = 7, conflict_policy: str = "reject",
    base_snapshot: Path | None = None, page_size: int = 500, max_pages: int = 10000,
    attempts: int = 4, backoff: float = 1, sleep=time.sleep, emit=None,
) -> dict:
    # All file updates in this module are local and serialized per output root.
    durable_directory(output_dir)
    audit_dir = output_dir / "ingestion-runs"
    durable_directory(audit_dir)
    run_id = uuid4().hex
    audit_path = audit_dir / f"{run_id}.json"
    audit = {"run_id": run_id, "started_at_utc": now(), "finished_at_utc": None,
             "status": "running", "requested_start": start.isoformat() if start else None,
             "requested_end": end.isoformat(), "overlap_days": overlap_days,
             "conflict_policy": conflict_policy, "base_snapshot_version": None,
             "published_snapshot_version": None, "failures": [],
             "routes": {r: {"status": "pending", "fetched_rows": 0,
                            "previous_rows": 0, "merged_rows": None,
                            "watermark_before": None, "watermark_after": None} for r in ROUTES}}
    began = time.monotonic()
    published = False
    publication_attempted = False
    pointer_temp = output_dir / f".current-{run_id}.json"
    for name in ["httpx", "httpcore", *logging.root.manager.loggerDict]:
        if name.startswith(("httpx", "httpcore")):
            logging.getLogger(name).disabled = True

    def save() -> None:
        atomic_json(audit_path, json.loads(safe_json(audit, key)))

    def report(event: str, **fields) -> None:
        item = json.loads(safe_json({"event": event, "run_id": run_id,
                                    "elapsed_seconds": round(time.monotonic() - began, 3), **fields}, key))
        if emit:
            emit(item)
        else:
            print(json.dumps(item, sort_keys=True), file=sys.stderr, flush=True)

    def progress(event: str, **fields) -> None:
        route = fields.get("route")
        if route and event == "page_complete":
            audit["routes"][route]["fetched_rows"] = fields["records"]
            save()
        report(event, **fields)

    with (output_dir / ".refresh.lock").open("a") as lock:
        try:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error("Another refresh owns the output writer lock.") from None
            current = None
            old = {r: {} for r in ROUTES}
            base_version = None
            save()  # Persist intent before reading/validating prior data or networking.
            if not key.strip():
                raise Error("Missing EIA_API_KEY.")
            if type(overlap_days) is not int or not 0 <= overlap_days <= 3660:
                raise Error("overlap_days must be between 0 and 3660.")
            if conflict_policy not in ("reject", "replace"):
                raise Error("Unknown conflict policy.")
            if not 1 <= page_size <= 5000 or not 1 <= max_pages <= 100000 or not 1 <= attempts <= 6:
                raise Error("Invalid page size, page limit, or retry budget.")
            if not 0 <= backoff <= 30:
                raise Error("Invalid backoff.")
            if (output_dir / "current.json").exists():
                if base_snapshot is not None:
                    raise Error("A base snapshot may only be adopted before the first publication.")
                current, old, base_version = load_snapshot(output_dir)
            elif base_snapshot is not None:
                current, old, base_version = load_snapshot(base_snapshot)
            reconcile_audits(audit_dir, current, base_version, run_id, report)
            audit["base_snapshot_version"] = base_version
            for route in ROUTES:
                audit["routes"][route]["previous_rows"] = len(old[route])
                watermark = current["watermarks"][route] if current else None
                audit["routes"][route].update(watermark_before=watermark, watermark_after=watermark)
            if current:
                if start is not None:
                    raise Error("--start is only valid for a first run without a base snapshot.")
                dataset_start = extraction.parse_date(current["start"])
                watermark = extraction.parse_date(current["end"])
                if end < watermark:
                    raise Error("Refresh end cannot rewind the published watermark.")
                start = max(dataset_start, watermark + timedelta(days=1 - overlap_days))
            else:
                if start is None:
                    raise Error("First refresh requires an explicit --start or a base snapshot.")
                dataset_start = start
            audit["requested_start"] = start.isoformat()
            if start > end:
                if not current:
                    raise Error("Start date must not follow end date.")
                audit.update(status="no_op", finished_at_utc=now(),
                             requested_start=None, next_start=start.isoformat())
                for route in ROUTES:
                    audit["routes"][route].update(status="no_op", merged_rows=len(old[route]))
                save()
                report("refresh_no_op")
                return audit
            save()
            report("refresh_started", start=start.isoformat(), end=end.isoformat())
            runs = output_dir / "runs"
            runs.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".staging-", dir=output_dir) as staging:
                stage = Path(staging)
                manifest = {"schema_version": 2, "snapshot_version": run_id,
                            "start": dataset_start.isoformat(), "end": end.isoformat(),
                            "created_at_utc": now(), "routes": {}, "watermarks": {}}
                for route in ROUTES:
                    entry = audit["routes"][route]
                    entry["status"] = "running"
                    save()
                    path = stage / f"{route}.parquet"
                    try:
                        fetched = extraction.extract_route(client, route, key, start, end, path,
                                                           page_size, max_pages, attempts, backoff, sleep, progress)
                        incoming = validated_rows(path, route, start, end)
                        merged, counts = merge_rows(old[route], incoming, start, end, conflict_policy)
                        rows = [merged[k] for k in sorted(merged)]
                        pq.write_table(pa.Table.from_pylist(rows, schema=extraction.schema_for(route)),
                                       path, compression="zstd")
                        checked = validated_rows(path, route, dataset_start, end)
                        if len(checked) != len(merged):
                            raise Error("Merged Parquet row count mismatch.")
                        with path.open("rb") as source:
                            os.fsync(source.fileno())
                        watermark = max(k[0] for k in checked).isoformat()
                        entry.update(status="validated", fetched_rows=fetched["records"],
                                     merged_rows=len(checked), **counts)
                        manifest["routes"][route] = {**fetched, "records": len(checked)}
                        manifest["watermarks"][route] = watermark
                    except Error as exc:
                        entry.update(status="failed", failure=str(exc))
                        audit["failures"].append(f"{route}: {exc}")
                        report("refresh_route_failed", route=route, reason=str(exc))
                    save()
                if audit["failures"]:
                    raise Error("Refresh failed validation; no snapshot published.")
                if set(manifest["watermarks"].values()) != {end.isoformat()}:
                    raise Error("Merged route watermarks disagree.")
                audit.update(status="published", finished_at_utc=now(), published_snapshot_version=run_id)
                for entry in audit["routes"].values():
                    entry["status"] = "published"
                    entry["watermark_after"] = end.isoformat()
                manifest["ingestion_run"] = json.loads(safe_json(audit, key))
                extraction.write_json(stage / "manifest.json", json.loads(safe_json(manifest, key)))
                extraction.sync_directory(stage)
                os.replace(stage, runs / run_id)
                extraction.sync_directory(runs)
                extraction.write_json(pointer_temp, {"run": f"runs/{run_id}"})
                publication_attempted = True
                os.replace(pointer_temp, output_dir / "current.json")
                published = True
                extraction.sync_directory(output_dir)
            try:
                save()
            except OSError:
                report("audit_update_failed", published=True)
            report("refresh_published", snapshot_version=run_id, watermarks=manifest["watermarks"])
            return manifest
        except (Exception, KeyboardInterrupt) as exc:
            if publication_attempted and not published:
                # A signal can arrive after rename succeeds but before assignment.
                # Never infer the commit outcome from the in-memory flag alone.
                pointer = output_dir / "current.json"
                if pointer.exists() and read_json(pointer).get("run") == f"runs/{run_id}":
                    committed, _, _ = load_snapshot(output_dir)
                    audit = committed["ingestion_run"]
                    published = True
                    extraction.sync_directory(output_dir)
            if published:
                try:
                    save()
                except OSError:
                    report("audit_update_failed", published=True)
            else:
                audit.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                             finished_at_utc=now(), published_snapshot_version=None)
                for entry in audit["routes"].values():
                    entry["watermark_after"] = entry["watermark_before"]
                    if entry["status"] == "published":
                        entry["status"] = "validated"
                    elif entry["status"] == "running":
                        entry["status"] = audit["status"]
                reason = str(exc) if isinstance(exc, Error) else f"Operation failed ({type(exc).__name__}); details withheld."
                if reason not in audit["failures"]:
                    audit["failures"].append(reason)
                try:
                    save()
                except OSError:
                    report("audit_update_failed", published=False)
            report("refresh_failed", published=published)
            raise
        finally:
            pointer_temp.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", help="Required only to bootstrap without a base snapshot")
    parser.add_argument("--end", required=True)
    parser.add_argument("--overlap-days", type=int, default=7)
    parser.add_argument("--conflict-policy", choices=("reject", "replace"), default="reject")
    parser.add_argument("--base-snapshot", type=Path, help="Existing window directory containing current.json")
    parser.add_argument("--output-dir", type=Path, default=Path("data/eia/incremental"))
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--max-pages", type=int, default=10000)
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--backoff", type=float, default=1)
    parser.add_argument("--timeout", type=int, choices=range(1, 61), default=30)
    args = parser.parse_args(argv)
    try:
        start = extraction.parse_date(args.start) if args.start else None
        end = extraction.parse_date(args.end)
        try:
            key = load_api_key(args.env_file)
        except ExplorationError:
            key = ""  # The engine records a failed attempt without networking.
        with httpx.Client(timeout=args.timeout, follow_redirects=False,
                          limits=httpx.Limits(max_keepalive_connections=0)) as client:
            refresh(client, key, end, args.output_dir, start=start, overlap_days=args.overlap_days,
                    conflict_policy=args.conflict_policy, base_snapshot=args.base_snapshot,
                    page_size=args.page_size, max_pages=args.max_pages, attempts=args.attempts,
                    backoff=args.backoff)
        return 0
    except (Error, ExplorationError) as exc:
        print(json.dumps({"event": "refresh_command_failed", "reason": str(exc)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, pa.ArrowException, httpx.RequestError):
        print(json.dumps({"event": "refresh_command_failed", "reason": "Local storage or transport operation failed."}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
