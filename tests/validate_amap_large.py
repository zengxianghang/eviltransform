#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Large-sample AMap validator for eviltransform.

This validator is intended for tens of thousands of points.  It reuses the
already smoke-tested C compile/transform helpers from validate_amap.py and adds:

- compact progress output;
- automatic retry/backoff for AMap QPS/minute rate limits;
- a persistent coordinate-keyed AMap reference cache;
- per-batch flush + fsync so interrupted runs can resume safely;
- top-N worst points while still writing the complete detailed CSV.

Only Python standard-library modules are used.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import validate_amap as base


MINUTE_LIMIT_BACKOFF_SEC = 61.0
QPS_BACKOFF_BASE_SEC = 1.0
QPS_RATE_LIMIT_CODES = {
    "10014",
    "10015",
    "10019",
    "10020",
    "10021",
    "10022",
    "10023",
}
DEFAULT_TOP_ERRORS = 20
DEFAULT_COMPARE_PROGRESS = 5000
DEFAULT_REQUEST_DELAY_SEC = 0.50
DEFAULT_RETRIES = 5
CACHE_FIELDS = ["wgs_lat", "wgs_lon", "amap_gcj_lat", "amap_gcj_lon"]


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    tests_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Large-sample eviltransform C vs AMap validation."
    )
    parser.add_argument("--points", type=Path, required=True)
    parser.add_argument("--c-source", type=Path, default=repo_root / "c" / "transform.c")
    parser.add_argument("--c-include", type=Path, default=repo_root / "c")
    parser.add_argument(
        "--output-dir", type=Path, default=tests_dir / "output" / "full"
    )
    parser.add_argument("--compiler", default=None)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SEC,
        help="delay between AMap requests in seconds (default: 0.50)",
    )
    parser.add_argument("--max-error-m", type=float, default=5.0)
    parser.add_argument("--top-errors", type=int, default=DEFAULT_TOP_ERRORS)
    parser.add_argument(
        "--compare-progress",
        type=int,
        default=DEFAULT_COMPARE_PROGRESS,
        help="print comparison progress every N points; 0 disables",
    )
    parser.add_argument(
        "--cache-file",
        type=Path,
        default=None,
        help="persistent AMap reference cache; default: <output-dir>/amap_reference_cache.csv",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="discard the existing cache before querying AMap",
    )
    return parser.parse_args()


def coord_key(point: dict[str, object]) -> tuple[str, str]:
    return (f"{float(point['lat']):.6f}", f"{float(point['lon']):.6f}")


def load_cache(path: Path) -> dict[tuple[str, str], tuple[float, float]]:
    cache: dict[tuple[str, str], tuple[float, float]] = {}
    if not path.is_file():
        return cache

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if set(CACHE_FIELDS) - set(reader.fieldnames or []):
            raise ValueError(
                f"cache file has incompatible columns: {path}; found {reader.fieldnames}"
            )
        for line_no, row in enumerate(reader, start=2):
            try:
                lat = f"{float(row['wgs_lat']):.6f}"
                lon = f"{float(row['wgs_lon']):.6f}"
                amap_lat = float(row["amap_gcj_lat"])
                amap_lon = float(row["amap_gcj_lon"])
            except (TypeError, ValueError, KeyError):
                print(
                    f"WARNING: ignoring malformed cache row {line_no} in {path}",
                    file=sys.stderr,
                )
                continue
            if not (math.isfinite(amap_lat) and math.isfinite(amap_lon)):
                print(
                    f"WARNING: ignoring non-finite cache row {line_no} in {path}",
                    file=sys.stderr,
                )
                continue
            cache[(lat, lon)] = (amap_lat, amap_lon)
    return cache


def append_cache_batch(
    path: Path,
    points: list[dict[str, object]],
    results: list[tuple[float, float]],
) -> None:
    if len(points) != len(results):
        raise ValueError("cache append point/result count mismatch")

    path.parent.mkdir(parents=True, exist_ok=True)
    need_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CACHE_FIELDS)
        if need_header:
            writer.writeheader()
        for point, (amap_lat, amap_lon) in zip(points, results):
            lat, lon = coord_key(point)
            writer.writerow(
                {
                    "wgs_lat": lat,
                    "wgs_lon": lon,
                    "amap_gcj_lat": f"{amap_lat:.10f}",
                    "amap_gcj_lon": f"{amap_lon:.10f}",
                }
            )
        f.flush()
        os.fsync(f.fileno())


def amap_convert_batch_large(
    points: list[dict[str, object]],
    key: str,
    timeout: float,
    retries: int,
) -> tuple[list[tuple[float, float]], int, int]:
    if not points:
        return [], 0, 0
    if len(points) > base.AMAP_MAX_BATCH:
        raise ValueError(f"AMap batch exceeds {base.AMAP_MAX_BATCH} points")

    locations = "|".join(
        f"{float(p['lon']):.6f},{float(p['lat']):.6f}" for p in points
    )
    params = urllib.parse.urlencode(
        {
            "key": key,
            "locations": locations,
            "coordsys": "gps",
            "output": "JSON",
        }
    )
    request = urllib.request.Request(
        f"{base.AMAP_URL}?{params}",
        headers={"User-Agent": "eviltransform-amap-validator/large-1.2"},
    )

    last_error: Exception | None = None
    request_attempts = 0
    rate_limit_events = 0

    for attempt in range(retries + 1):
        request_attempts += 1
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)

            if data.get("status") != "1":
                info = str(data.get("info", "unknown error"))
                infocode = str(data.get("infocode", "unknown"))

                if infocode == "10004":
                    rate_limit_events += 1
                    last_error = RuntimeError(
                        f"AMap minute-window rate limit: {info} (infocode={infocode})"
                    )
                    if attempt < retries:
                        print(
                            "AMap minute-window rate limit (10004); "
                            f"backing off {MINUTE_LIMIT_BACKOFF_SEC:.0f}s before retry...",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(MINUTE_LIMIT_BACKOFF_SEC)
                        continue

                if infocode in QPS_RATE_LIMIT_CODES:
                    rate_limit_events += 1
                    last_error = RuntimeError(
                        f"AMap QPS rate limit: {info} (infocode={infocode})"
                    )
                    if attempt < retries:
                        backoff = QPS_BACKOFF_BASE_SEC * (2**attempt)
                        print(
                            f"AMap QPS rate limit ({infocode}); "
                            f"backing off {backoff:.1f}s before retry...",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(backoff)
                        continue

                raise RuntimeError(f"AMap API error: {info} (infocode={infocode})")

            locations_out = data.get("locations")
            if not isinstance(locations_out, str):
                raise RuntimeError(f"AMap response has no locations field: {payload}")

            output_pairs = locations_out.split(";")
            if len(output_pairs) != len(points):
                raise RuntimeError(
                    f"AMap returned {len(output_pairs)} points for {len(points)} inputs"
                )

            result: list[tuple[float, float]] = []
            for item in output_pairs:
                parts = item.split(",")
                if len(parts) != 2:
                    raise RuntimeError(f"unexpected AMap coordinate: {item!r}")
                result.append((float(parts[1]), float(parts[0])))
            return result, request_attempts, rate_limit_events

        except RuntimeError:
            raise
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (2**attempt))

    raise RuntimeError(
        f"AMap request failed after {retries + 1} attempts: {last_error}"
    )


def amap_convert_all_cached(
    points: list[dict[str, object]],
    key: str,
    timeout: float,
    retries: int,
    request_delay: float,
    cache_path: Path,
) -> tuple[list[tuple[float, float]], int, int, int, int]:
    cache = load_cache(cache_path)
    cached_before = sum(1 for point in points if coord_key(point) in cache)
    missing = [point for point in points if coord_key(point) not in cache]

    print(f"AMap cache   : {cache_path}")
    print(f"Cache hits   : {cached_before}/{len(points)}")
    print(f"Need fetch   : {len(missing)} point(s)")

    total_batches = math.ceil(len(missing) / base.AMAP_MAX_BATCH) if missing else 0
    progress_step = max(1, total_batches // 20) if total_batches else 1
    request_attempts = 0
    rate_limit_events = 0
    fetched_points = 0

    for batch_index, start in enumerate(
        range(0, len(missing), base.AMAP_MAX_BATCH), start=1
    ):
        batch = missing[start : start + base.AMAP_MAX_BATCH]
        batch_result, batch_requests, batch_rate_limits = amap_convert_batch_large(
            batch, key, timeout, retries
        )

        # Persist each successful batch before doing any further network work.
        append_cache_batch(cache_path, batch, batch_result)
        for point, result in zip(batch, batch_result):
            cache[coord_key(point)] = result

        request_attempts += batch_requests
        rate_limit_events += batch_rate_limits
        fetched_points += len(batch)

        if (
            batch_index == 1
            or batch_index == total_batches
            or batch_index % progress_step == 0
        ):
            print(
                f"AMap fetch progress: {batch_index}/{total_batches} batches, "
                f"{fetched_points}/{len(missing)} missing points fetched",
                flush=True,
            )

        if request_delay > 0 and batch_index < total_batches:
            time.sleep(request_delay)

    result: list[tuple[float, float]] = []
    for point in points:
        cached = cache.get(coord_key(point))
        if cached is None:
            raise RuntimeError(f"internal cache miss after fetch: {coord_key(point)}")
        result.append(cached)

    return result, request_attempts, rate_limit_events, cached_before, fetched_points


def main() -> int:
    args = parse_args()

    try:
        key = os.environ.get("AMAP_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "AMAP_KEY environment variable is not set. "
                "On macOS: export AMAP_KEY=\"your-web-service-key\""
            )

        points = base.read_points(args.points)
        compiler = base.find_compiler(args.compiler)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        cache_path = args.cache_file or (args.output_dir / "amap_reference_cache.csv")

        if args.refresh_cache and cache_path.exists():
            cache_path.unlink()
            print(f"Cache refresh : removed {cache_path}")

        print(f"Test points  : {len(points)}")
        print(f"C source     : {args.c_source}")
        print(f"Compiler     : {compiler}")
        print("AMap key     : [set; hidden]")
        print("Console      : compact large-sample mode")
        print(f"Request delay: {max(0.0, args.request_delay):.3f} s")
        print(f"Retries      : {max(0, args.retries)}")
        print()

        with tempfile.TemporaryDirectory(prefix="eviltransform_amap_large_") as tmp:
            exe = base.build_c_runner(
                compiler, args.c_source, args.c_include, Path(tmp)
            )
            evil_results = base.run_c_transform(exe, points)
        print(f"C transform  : completed {len(evil_results)} points")

        (
            amap_results,
            request_attempts,
            rate_limit_events,
            cache_hits,
            fetched_points,
        ) = amap_convert_all_cached(
            points,
            key,
            args.timeout,
            max(0, args.retries),
            max(0.0, args.request_delay),
            cache_path,
        )

        if len(amap_results) != len(evil_results):
            raise RuntimeError("internal result count mismatch")

        detail_rows: list[dict[str, object]] = []
        errors: list[float] = []
        worst_rows: list[tuple[float, str, float, float]] = []
        progress_every = max(0, args.compare_progress)

        for index, (point, evil, amap) in enumerate(
            zip(points, evil_results, amap_results), start=1
        ):
            lat = float(point["lat"])
            lon = float(point["lon"])
            evil_lat, evil_lon = evil
            amap_lat, amap_lon = amap
            diff_lat = evil_lat - amap_lat
            diff_lon = evil_lon - amap_lon
            error_m = base.haversine_m(evil_lat, evil_lon, amap_lat, amap_lon)
            errors.append(error_m)
            worst_rows.append((error_m, str(point["name"]), lat, lon))
            detail_rows.append(
                {
                    "name": point["name"],
                    "wgs_lat": f"{lat:.6f}",
                    "wgs_lon": f"{lon:.6f}",
                    "evil_gcj_lat": f"{evil_lat:.10f}",
                    "evil_gcj_lon": f"{evil_lon:.10f}",
                    "amap_gcj_lat": f"{amap_lat:.10f}",
                    "amap_gcj_lon": f"{amap_lon:.10f}",
                    "diff_lat_deg": f"{diff_lat:.12f}",
                    "diff_lon_deg": f"{diff_lon:.12f}",
                    "error_m": f"{error_m:.6f}",
                }
            )
            if progress_every and (
                index % progress_every == 0 or index == len(points)
            ):
                print(f"Compare progress: {index}/{len(points)}", flush=True)

        mean_error = statistics.mean(errors)
        rms_error = math.sqrt(sum(e * e for e in errors) / len(errors))
        p95 = base.percentile_nearest_rank(errors, 0.95)
        p99 = base.percentile_nearest_rank(errors, 0.99)
        max_error = max(errors)
        worst_index = errors.index(max_error)
        worst_name = str(points[worst_index]["name"])
        threshold_enabled = args.max_error_m > 0
        passed = (not threshold_enabled) or max_error <= args.max_error_m

        result_path = args.output_dir / "amap_compare_result.csv"
        summary_path = args.output_dir / "amap_compare_summary.csv"
        base.write_results(result_path, detail_rows)

        summary: list[tuple[str, object]] = [
            ("count", len(errors)),
            ("mean_error_m", f"{mean_error:.6f}"),
            ("rms_error_m", f"{rms_error:.6f}"),
            ("p95_error_m", f"{p95:.6f}"),
            ("p99_error_m", f"{p99:.6f}"),
            ("max_error_m", f"{max_error:.6f}"),
            ("worst_point", worst_name),
            (
                "max_error_threshold_m",
                f"{args.max_error_m:.6f}" if threshold_enabled else "disabled",
            ),
            ("validation", "PASS" if passed else "FAIL"),
            ("amap_input_decimals", base.AMAP_INPUT_DECIMALS),
            ("amap_batch_size", base.AMAP_MAX_BATCH),
            ("amap_request_delay_sec", f"{max(0.0, args.request_delay):.3f}"),
            ("amap_request_attempts_this_run", request_attempts),
            ("amap_rate_limit_events_this_run", rate_limit_events),
            ("amap_cache_file", str(cache_path)),
            ("amap_cache_hits_at_start", cache_hits),
            ("amap_points_fetched_this_run", fetched_points),
            ("compiler", compiler),
            ("c_source", str(args.c_source)),
            ("c_source_sha256", base.sha256_file(args.c_source)),
        ]
        base.write_summary(summary_path, summary)

        print()
        print("========== Summary ==========")
        print(f"Count : {len(errors)}")
        print(f"Mean  : {mean_error:.3f} m")
        print(f"RMS   : {rms_error:.3f} m")
        print(f"P95   : {p95:.3f} m")
        print(f"P99   : {p99:.3f} m")
        print(f"Max   : {max_error:.3f} m ({worst_name})")
        print(f"Cache hits at start          : {cache_hits}")
        print(f"AMap points fetched this run : {fetched_points}")
        print(f"AMap requests incl. retries  : {request_attempts}")
        print(f"Rate-limit events this run   : {rate_limit_events}")
        if threshold_enabled:
            print(f"Limit : {args.max_error_m:.3f} m")
        print(f"Result: {'PASS' if passed else 'FAIL'}")

        top_n = min(max(0, args.top_errors), len(worst_rows))
        if top_n:
            print()
            print(f"========== Worst {top_n} points ==========")
            print(f"{'Name':<14} {'Error(m)':>10} {'WGS lat':>12} {'WGS lon':>13}")
            for error_m, name, lat, lon in sorted(worst_rows, reverse=True)[:top_n]:
                print(f"{name:<14} {error_m:10.3f} {lat:12.6f} {lon:13.6f}")

        print()
        print(f"AMap cache  : {cache_path}")
        print(f"Detailed CSV: {result_path}")
        print(f"Summary CSV : {summary_path}")
        return 0 if passed else 2

    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
