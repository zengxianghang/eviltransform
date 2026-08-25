#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Large-sample AMap validator for eviltransform.

This module reuses the already smoke-tested C compile/transform and CSV helpers
from validate_amap.py, but changes console behavior and AMap retry handling for
large data sets:

- compact progress instead of printing every point / batch;
- automatic retry for AMap 10004 ACCESS_TOO_FREQUENT;
- top-N worst points printed at the end;
- full detailed CSV is still written.

Only Python standard-library modules are used.
"""

from __future__ import annotations

import argparse
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


RATE_LIMIT_BACKOFF_SEC = 61.0
DEFAULT_TOP_ERRORS = 20
DEFAULT_COMPARE_PROGRESS = 5000


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    tests_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Large-sample eviltransform C vs AMap validation."
    )
    parser.add_argument(
        "--points",
        type=Path,
        required=True,
        help="input CSV with name,wgs_lat,wgs_lon",
    )
    parser.add_argument(
        "--c-source",
        type=Path,
        default=repo_root / "c" / "transform.c",
    )
    parser.add_argument(
        "--c-include",
        type=Path,
        default=repo_root / "c",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=tests_dir / "output" / "full",
    )
    parser.add_argument("--compiler", default=None)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--request-delay", type=float, default=0.10)
    parser.add_argument("--max-error-m", type=float, default=5.0)
    parser.add_argument("--top-errors", type=int, default=DEFAULT_TOP_ERRORS)
    parser.add_argument(
        "--compare-progress",
        type=int,
        default=DEFAULT_COMPARE_PROGRESS,
        help="print comparison progress every N points; 0 disables",
    )
    return parser.parse_args()


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
        headers={"User-Agent": "eviltransform-amap-validator/large-1.0"},
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
                        f"AMap API rate limited: {info} (infocode={infocode})"
                    )
                    if attempt < retries:
                        print(
                            "AMap rate limit (10004); backing off before retry...",
                            file=sys.stderr,
                            flush=True,
                        )
                        time.sleep(RATE_LIMIT_BACKOFF_SEC)
                        continue

                raise RuntimeError(
                    f"AMap API error: {info} (infocode={infocode})"
                )

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


def amap_convert_all_large(
    points: list[dict[str, object]],
    key: str,
    timeout: float,
    retries: int,
    request_delay: float,
) -> tuple[list[tuple[float, float]], int, int]:
    result: list[tuple[float, float]] = []
    total_batches = math.ceil(len(points) / base.AMAP_MAX_BATCH)
    progress_step = max(1, total_batches // 20)
    request_attempts = 0
    rate_limit_events = 0

    for batch_index, start in enumerate(
        range(0, len(points), base.AMAP_MAX_BATCH), start=1
    ):
        batch = points[start : start + base.AMAP_MAX_BATCH]
        batch_result, batch_requests, batch_rate_limits = amap_convert_batch_large(
            batch,
            key,
            timeout,
            retries,
        )
        result.extend(batch_result)
        request_attempts += batch_requests
        rate_limit_events += batch_rate_limits

        if (
            batch_index == 1
            or batch_index == total_batches
            or batch_index % progress_step == 0
        ):
            done_points = min(batch_index * base.AMAP_MAX_BATCH, len(points))
            print(
                f"AMap progress: {batch_index}/{total_batches} batches, "
                f"{done_points}/{len(points)} points",
                flush=True,
            )

        if request_delay > 0 and batch_index < total_batches:
            time.sleep(request_delay)

    return result, request_attempts, rate_limit_events


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

        print(f"Test points : {len(points)}")
        print(f"C source    : {args.c_source}")
        print(f"Compiler    : {compiler}")
        print("AMap key    : [set; hidden]")
        print("Console     : compact large-sample mode")
        print()

        with tempfile.TemporaryDirectory(prefix="eviltransform_amap_large_") as tmp:
            exe = base.build_c_runner(
                compiler,
                args.c_source,
                args.c_include,
                Path(tmp),
            )
            evil_results = base.run_c_transform(exe, points)

        print(f"C transform : completed {len(evil_results)} points")

        amap_results, request_attempts, rate_limit_events = amap_convert_all_large(
            points,
            key,
            args.timeout,
            max(0, args.retries),
            max(0.0, args.request_delay),
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
            error_m = base.haversine_m(
                evil_lat, evil_lon, amap_lat, amap_lon
            )
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
            ("amap_request_attempts", request_attempts),
            ("amap_rate_limit_events", rate_limit_events),
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
        print(f"AMap requests (incl. retries): {request_attempts}")
        print(f"Rate-limit events           : {rate_limit_events}")
        if threshold_enabled:
            print(f"Limit : {args.max_error_m:.3f} m")
        print(f"Result: {'PASS' if passed else 'FAIL'}")

        top_n = min(max(0, args.top_errors), len(worst_rows))
        if top_n:
            print()
            print(f"========== Worst {top_n} points ==========")
            print(f"{'Name':<14} {'Error(m)':>10} {'WGS lat':>12} {'WGS lon':>13}")
            for error_m, name, lat, lon in sorted(
                worst_rows, reverse=True
            )[:top_n]:
                print(f"{name:<14} {error_m:10.3f} {lat:12.6f} {lon:13.6f}")

        print()
        print(f"Detailed CSV: {result_path}")
        print(f"Summary CSV : {summary_path}")

        return 0 if passed else 2

    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
