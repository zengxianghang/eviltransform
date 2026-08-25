#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate eviltransform C WGS84->GCJ-02 output against AMap.

Only Python standard-library modules are used.

Workflow:
  1. Read WGS84 test points from CSV.
  2. Normalize input coordinates to 6 decimal places, matching AMap's
     documented coordinate-conversion input precision.
  3. Compile the repository's current c/transform.c with a temporary C harness.
  4. Run wgs2gcj() for every normalized test point.
  5. Query AMap's coordinate conversion service with coordsys=gps, in batches
     of at most 40 coordinate pairs per request.
  6. Compare the two GCJ-02 results.
  7. Write detailed and summary CSV reports.

The AMap key is read from the AMAP_KEY environment variable and is never
written to output files.

Examples:

    # PowerShell
    $env:AMAP_KEY="your-key"
    python tests/validate_amap.py

    # cmd.exe
    set AMAP_KEY=your-key
    python tests\validate_amap.py

    # Select compiler / threshold
    python tests/validate_amap.py --compiler gcc --max-error-m 5

Exit codes:
    0: validation passed, or threshold checking was disabled
    1: setup, compilation, file, network, or AMap API failure
    2: validation completed but max error exceeded --max-error-m
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


EARTH_R = 6378137.0
AMAP_URL = "https://restapi.amap.com/v3/assistant/coordinate/convert"
AMAP_MAX_BATCH = 40
AMAP_INPUT_DECIMALS = 6
DEFAULT_MAX_ERROR_M = 5.0


def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    tests_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description=(
            "Compile eviltransform's C implementation and compare its "
            "WGS84->GCJ-02 output with AMap."
        )
    )
    parser.add_argument(
        "--points",
        type=Path,
        default=tests_dir / "amap_test_points.csv",
        help="input CSV with name,wgs_lat,wgs_lon",
    )
    parser.add_argument(
        "--c-source",
        type=Path,
        default=repo_root / "c" / "transform.c",
        help="eviltransform C source to compile",
    )
    parser.add_argument(
        "--c-include",
        type=Path,
        default=repo_root / "c",
        help="directory containing transform.h",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=tests_dir / "output",
        help="directory for generated CSV files",
    )
    parser.add_argument(
        "--compiler",
        default=None,
        help="compiler executable; otherwise CC, cc, gcc, clang, cl are tried",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="AMap HTTP timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="network retries per AMap request (default: 2)",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.05,
        help="delay between AMap batches in seconds (default: 0.05)",
    )
    parser.add_argument(
        "--max-error-m",
        type=float,
        default=DEFAULT_MAX_ERROR_M,
        help=(
            "exit 2 if max error exceeds this value; "
            "use 0 to disable threshold checking (default: 5 m)"
        ),
    )
    return parser.parse_args()


def read_points(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        raise FileNotFoundError(f"test point file not found: {path}")

    required = {"name", "wgs_lat", "wgs_lon"}
    points: list[dict[str, object]] = []

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        missing = required - fields
        if missing:
            raise ValueError(
                f"{path}: missing required CSV columns: {', '.join(sorted(missing))}; "
                f"found: {reader.fieldnames}"
            )

        names: set[str] = set()
        for line_no, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            if not name:
                raise ValueError(f"{path}:{line_no}: empty name")
            if name in names:
                raise ValueError(f"{path}:{line_no}: duplicate name: {name}")

            try:
                lat = float(row["wgs_lat"])
                lon = float(row["wgs_lon"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_no}: invalid latitude/longitude") from exc

            if not (math.isfinite(lat) and math.isfinite(lon)):
                raise ValueError(f"{path}:{line_no}: non-finite latitude/longitude")
            if not -90.0 <= lat <= 90.0:
                raise ValueError(f"{path}:{line_no}: latitude out of range: {lat}")
            if not -180.0 <= lon <= 180.0:
                raise ValueError(f"{path}:{line_no}: longitude out of range: {lon}")

            # AMap's Web Service coordinate conversion API documents a maximum
            # of 6 digits after the decimal point. Use the exact same rounded
            # coordinates for both eviltransform and AMap.
            lat = round(lat, AMAP_INPUT_DECIMALS)
            lon = round(lon, AMAP_INPUT_DECIMALS)

            names.add(name)
            points.append({"name": name, "lat": lat, "lon": lon})

    if not points:
        raise ValueError(f"{path}: no test points")

    return points


def find_compiler(explicit: str | None) -> str:
    if explicit:
        candidates = [explicit]
    elif os.environ.get("CC"):
        candidates = [os.environ["CC"]]
    else:
        candidates = ["cc", "gcc", "clang", "cl"]

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    raise RuntimeError(
        "no C compiler found. Tried: "
        + ", ".join(candidates)
        + ". Install gcc/clang, run from a Visual Studio Developer Command Prompt, "
        "or pass --compiler / set CC."
    )


def compiler_is_msvc(compiler: str) -> bool:
    return Path(compiler).name.lower() in {"cl", "cl.exe"}


def build_c_runner(
    compiler: str,
    c_source: Path,
    include_dir: Path,
    build_dir: Path,
) -> Path:
    if not c_source.is_file():
        raise FileNotFoundError(f"C source not found: {c_source}")
    if not (include_dir / "transform.h").is_file():
        raise FileNotFoundError(f"transform.h not found in: {include_dir}")

    harness = build_dir / "amap_validation_runner.c"
    exe = build_dir / (
        "amap_validation_runner.exe" if os.name == "nt" else "amap_validation_runner"
    )

    harness.write_text(
        "#include <stdio.h>\n"
        "#include \"transform.h\"\n"
        "\n"
        "int main(void) {\n"
        "    double lat, lon;\n"
        "    while (scanf(\"%lf %lf\", &lat, &lon) == 2) {\n"
        "        double gcjLat, gcjLon;\n"
        "        wgs2gcj(lat, lon, &gcjLat, &gcjLon);\n"
        "        printf(\"%.15f %.15f\\n\", gcjLat, gcjLon);\n"
        "    }\n"
        "    return 0;\n"
        "}\n",
        encoding="utf-8",
    )

    if compiler_is_msvc(compiler):
        cmd = [
            compiler,
            "/nologo",
            "/O2",
            "/D_USE_MATH_DEFINES",
            f"/I{include_dir}",
            str(harness),
            str(c_source),
            f"/Fe:{exe}",
        ]
    else:
        cmd = [
            compiler,
            "-O2",
            "-D_GNU_SOURCE",
            f"-I{include_dir}",
            str(harness),
            str(c_source),
            "-lm",
            "-o",
            str(exe),
        ]

    proc = subprocess.run(
        cmd,
        cwd=build_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not exe.is_file():
        raise RuntimeError(
            "C compilation failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )

    return exe


def run_c_transform(
    exe: Path,
    points: list[dict[str, object]],
) -> list[tuple[float, float]]:
    stdin_text = "".join(
        f"{float(p['lat']):.6f} {float(p['lon']):.6f}\n" for p in points
    )
    proc = subprocess.run(
        [str(exe)],
        input=stdin_text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"compiled C runner failed with code {proc.returncode}\n"
            f"stderr:\n{proc.stderr}"
        )

    rows = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(rows) != len(points):
        raise RuntimeError(
            f"C runner returned {len(rows)} rows for {len(points)} input points"
        )

    result: list[tuple[float, float]] = []
    for i, line in enumerate(rows, start=1):
        parts = line.split()
        if len(parts) != 2:
            raise RuntimeError(f"invalid C runner output at row {i}: {line!r}")
        result.append((float(parts[0]), float(parts[1])))
    return result


def amap_convert_batch(
    points: list[dict[str, object]],
    key: str,
    timeout: float,
    retries: int,
) -> list[tuple[float, float]]:
    if not points:
        return []
    if len(points) > AMAP_MAX_BATCH:
        raise ValueError(f"AMap batch exceeds {AMAP_MAX_BATCH} points")

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
        f"{AMAP_URL}?{params}",
        headers={"User-Agent": "eviltransform-amap-validator/1.0"},
    )

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)

            if data.get("status") != "1":
                info = data.get("info", "unknown error")
                infocode = data.get("infocode", "unknown")
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
                gcj_lon = float(parts[0])
                gcj_lat = float(parts[1])
                result.append((gcj_lat, gcj_lon))
            return result

        except RuntimeError:
            # API errors such as invalid keys or quota failures should not be retried.
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


def amap_convert_all(
    points: list[dict[str, object]],
    key: str,
    timeout: float,
    retries: int,
    request_delay: float,
) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    total_batches = math.ceil(len(points) / AMAP_MAX_BATCH)

    for batch_index, start in enumerate(range(0, len(points), AMAP_MAX_BATCH), start=1):
        batch = points[start : start + AMAP_MAX_BATCH]
        print(
            f"AMap batch {batch_index}/{total_batches}: "
            f"{len(batch)} point(s)"
        )
        result.extend(amap_convert_batch(batch, key, timeout, retries))
        if request_delay > 0 and batch_index < total_batches:
            time.sleep(request_delay)

    return result


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    )
    a = min(1.0, max(0.0, a))
    return 2.0 * EARTH_R * math.asin(math.sqrt(a))


def percentile_nearest_rank(values: list[float], p: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    rank = max(1, math.ceil(p * len(ordered)))
    return ordered[rank - 1]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_results(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "name",
        "wgs_lat",
        "wgs_lon",
        "evil_gcj_lat",
        "evil_gcj_lon",
        "amap_gcj_lat",
        "amap_gcj_lon",
        "diff_lat_deg",
        "diff_lon_deg",
        "error_m",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, summary: list[tuple[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        writer.writerows(summary)


def main() -> int:
    args = parse_args()

    try:
        key = os.environ.get("AMAP_KEY", "").strip()
        if not key:
            raise RuntimeError(
                "AMAP_KEY environment variable is not set. "
                "Do not put the key in this script."
            )

        points = read_points(args.points)
        compiler = find_compiler(args.compiler)
        args.output_dir.mkdir(parents=True, exist_ok=True)

        print(f"Test points : {len(points)}")
        print(f"C source    : {args.c_source}")
        print(f"Compiler    : {compiler}")
        print("AMap key    : [set; hidden]")
        print("Input       : normalized to 6 decimal places")
        print()

        with tempfile.TemporaryDirectory(prefix="eviltransform_amap_") as tmp:
            build_dir = Path(tmp)
            exe = build_c_runner(compiler, args.c_source, args.c_include, build_dir)
            evil_results = run_c_transform(exe, points)

        amap_results = amap_convert_all(
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

        print()
        print(f"{'Name':<20} {'Error(m)':>10}")
        print("-" * 31)

        for point, evil, amap in zip(points, evil_results, amap_results):
            lat = float(point["lat"])
            lon = float(point["lon"])
            evil_lat, evil_lon = evil
            amap_lat, amap_lon = amap

            diff_lat = evil_lat - amap_lat
            diff_lon = evil_lon - amap_lon
            error_m = haversine_m(evil_lat, evil_lon, amap_lat, amap_lon)
            errors.append(error_m)

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
            print(f"{str(point['name']):<20} {error_m:10.3f}")

        mean_error = statistics.mean(errors)
        rms_error = math.sqrt(sum(e * e for e in errors) / len(errors))
        p95 = percentile_nearest_rank(errors, 0.95)
        p99 = percentile_nearest_rank(errors, 0.99)
        max_error = max(errors)
        worst_index = errors.index(max_error)
        worst_name = str(points[worst_index]["name"])

        threshold_enabled = args.max_error_m > 0
        passed = (not threshold_enabled) or max_error <= args.max_error_m

        result_path = args.output_dir / "amap_compare_result.csv"
        summary_path = args.output_dir / "amap_compare_summary.csv"
        write_results(result_path, detail_rows)

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
            ("amap_input_decimals", AMAP_INPUT_DECIMALS),
            ("amap_batch_size", AMAP_MAX_BATCH),
            ("compiler", compiler),
            ("c_source", str(args.c_source)),
            ("c_source_sha256", sha256_file(args.c_source)),
        ]
        write_summary(summary_path, summary)

        print()
        print("========== Summary ==========")
        print(f"Count : {len(errors)}")
        print(f"Mean  : {mean_error:.3f} m")
        print(f"RMS   : {rms_error:.3f} m")
        print(f"P95   : {p95:.3f} m")
        print(f"P99   : {p99:.3f} m")
        print(f"Max   : {max_error:.3f} m ({worst_name})")
        if threshold_enabled:
            print(f"Limit : {args.max_error_m:.3f} m")
        print(f"Result: {'PASS' if passed else 'FAIL'}")
        print()
        print(f"Detailed CSV: {result_path}")
        print(f"Summary CSV : {summary_path}")

        return 0 if passed else 2

    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
