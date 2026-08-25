#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run staged AMap validation for eviltransform.

This is a thin wrapper around validate_amap.py.

Modes:
  smoke: Run a small fixed set of representative points first.
  full:  Run a larger comparison only when explicitly requested.

Full strategies:
  random (default): generate deterministic, approximately area-uniform random
                    points inside an approximate GCJ distortion-scope polygon.
  grid:             keep the older local-grid strategy around city anchors.

Only Python standard-library modules are used.

Examples on macOS:

    export AMAP_KEY="your-web-service-key"

    # Fast sanity check first (default mode)
    python3 tests/run_amap_validation.py --mode smoke

    # Nationwide-style deterministic random validation: 50,000 points
    python3 tests/run_amap_validation.py --mode full

    # Reproduce exactly the same random sample explicitly
    python3 tests/run_amap_validation.py --mode full --random-count 50000 --seed 20260825

AMap currently accepts at most 40 coordinate pairs per conversion request, so
50,000 points require 1,250 API requests.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from pathlib import Path
import subprocess
import sys

from gcj_scope import contains_gcj_scope, scope_bounds


DEFAULT_RANDOM_COUNT = 50_000
DEFAULT_RANDOM_SEED = 20260825
DEFAULT_GRID_RADIUS = 2
DEFAULT_GRID_STEP_DEG = 0.05
AMAP_BATCH_SIZE = 40
MASK64 = (1 << 64) - 1


class SplitMix64:
    """Small fixed PRNG so generated test coordinates are reproducible."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def random(self) -> float:
        # Use the upper 53 bits, matching double-precision mantissa width.
        return (self.next_u64() >> 11) * (1.0 / (1 << 53))


def parse_args() -> argparse.Namespace:
    tests_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="Run smoke or full AMap validation for eviltransform C."
    )
    parser.add_argument(
        "--mode",
        choices=("smoke", "full"),
        default="smoke",
        help="validation stage; default: smoke",
    )
    parser.add_argument(
        "--full-strategy",
        choices=("random", "grid"),
        default="random",
        help="full-mode sampling strategy; default: random",
    )
    parser.add_argument(
        "--random-count",
        type=int,
        default=DEFAULT_RANDOM_COUNT,
        help="full random mode: accepted random points; default: 50000",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_RANDOM_SEED,
        help="full random mode: deterministic 64-bit seed; default: 20260825",
    )
    parser.add_argument(
        "--compiler",
        default=None,
        help="optional compiler passed to validate_amap.py; macOS normally auto-detects cc/clang",
    )
    parser.add_argument(
        "--max-error-m",
        type=float,
        default=5.0,
        help="maximum allowed error in meters; 0 disables threshold checking",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="AMap HTTP timeout in seconds",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="network/API transient retries per AMap request",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.10,
        help="delay between AMap request batches in seconds; default: 0.10",
    )
    parser.add_argument(
        "--grid-radius",
        type=int,
        default=DEFAULT_GRID_RADIUS,
        help=(
            "full grid mode only: number of grid steps around each anchor; "
            "2 means a 5x5 grid"
        ),
    )
    parser.add_argument(
        "--grid-step-deg",
        type=float,
        default=DEFAULT_GRID_STEP_DEG,
        help="full grid mode only: latitude/longitude grid spacing in degrees",
    )
    parser.add_argument(
        "--smoke-points",
        type=Path,
        default=tests_dir / "amap_smoke_points.csv",
        help="smoke-mode point CSV",
    )
    parser.add_argument(
        "--full-anchors",
        type=Path,
        default=tests_dir / "amap_test_points.csv",
        help="full grid mode: anchor CSV",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=tests_dir / "output",
        help="root directory for generated validation outputs",
    )
    return parser.parse_args()


def read_points(path: Path) -> list[tuple[str, float, float]]:
    if not path.is_file():
        raise FileNotFoundError(f"point file not found: {path}")

    required = {"name", "wgs_lat", "wgs_lon"}
    points: list[tuple[str, float, float]] = []

    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        missing = required - fields
        if missing:
            raise ValueError(
                f"{path}: missing required columns: {', '.join(sorted(missing))}"
            )

        for line_no, row in enumerate(reader, start=2):
            name = (row.get("name") or "").strip()
            if not name:
                raise ValueError(f"{path}:{line_no}: empty name")
            try:
                lat = float(row["wgs_lat"])
                lon = float(row["wgs_lon"])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{path}:{line_no}: invalid latitude/longitude"
                ) from exc
            if not (math.isfinite(lat) and math.isfinite(lon)):
                raise ValueError(f"{path}:{line_no}: non-finite coordinate")
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                raise ValueError(f"{path}:{line_no}: coordinate out of range")
            points.append((name, lat, lon))

    if not points:
        raise ValueError(f"{path}: no points")

    return points


def write_random_points(path: Path, count: int, seed: int) -> tuple[int, int]:
    """Write deterministic random points inside the approximate GCJ scope.

    Longitude is sampled uniformly. Latitude is sampled uniformly in sin(lat),
    then rejected by the polygon. This approximates uniform sampling by surface
    area rather than over-weighting high latitudes.

    Coordinates are rounded to 6 decimals before the polygon check and de-dupe,
    matching the AMap coordinate-conversion input precision.
    """
    if count <= 0:
        raise ValueError("--random-count must be > 0")

    min_lat, max_lat, min_lon, max_lon = scope_bounds()
    sin_min = math.sin(math.radians(min_lat))
    sin_max = math.sin(math.radians(max_lat))
    rng = SplitMix64(seed)
    seen: set[tuple[float, float]] = set()
    attempts = 0
    max_attempts = max(100_000, count * 20)

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "wgs_lat", "wgs_lon"])

        while len(seen) < count:
            attempts += 1
            if attempts > max_attempts:
                raise RuntimeError(
                    f"unable to generate {count} random in-scope points after "
                    f"{attempts} attempts"
                )

            lon = min_lon + (max_lon - min_lon) * rng.random()
            sin_lat = sin_min + (sin_max - sin_min) * rng.random()
            lat = math.degrees(math.asin(sin_lat))

            # AMap's conversion API accepts at most 6 digits after decimal.
            lat = round(lat, 6)
            lon = round(lon, 6)

            if not contains_gcj_scope(lat, lon):
                continue

            key = (lat, lon)
            if key in seen:
                continue

            seen.add(key)
            index = len(seen)
            writer.writerow(
                [
                    f"Random{index:05d}",
                    f"{lat:.6f}",
                    f"{lon:.6f}",
                ]
            )

    return len(seen), attempts


def write_full_grid(
    anchors: list[tuple[str, float, float]],
    path: Path,
    radius: int,
    step_deg: float,
) -> int:
    if radius < 0:
        raise ValueError("--grid-radius must be >= 0")
    if not math.isfinite(step_deg) or step_deg <= 0:
        raise ValueError("--grid-step-deg must be > 0")

    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "wgs_lat", "wgs_lon"])

        for anchor_name, anchor_lat, anchor_lon in anchors:
            for iy in range(-radius, radius + 1):
                for ix in range(-radius, radius + 1):
                    lat = anchor_lat + iy * step_deg
                    lon = anchor_lon + ix * step_deg
                    name = f"{anchor_name}_dy{iy:+d}_dx{ix:+d}"
                    writer.writerow([name, f"{lat:.6f}", f"{lon:.6f}"])
                    count += 1

    return count


def write_manifest(
    path: Path,
    rows: list[tuple[str, object]],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["parameter", "value"])
        writer.writerows(rows)


def build_core_command(
    args: argparse.Namespace,
    points_path: Path,
    output_dir: Path,
) -> list[str]:
    tests_dir = Path(__file__).resolve().parent
    core = tests_dir / "validate_amap.py"
    if not core.is_file():
        raise FileNotFoundError(f"core validator not found: {core}")

    cmd = [
        sys.executable,
        str(core),
        "--points",
        str(points_path),
        "--output-dir",
        str(output_dir),
        "--max-error-m",
        str(args.max_error_m),
        "--timeout",
        str(args.timeout),
        "--retries",
        str(max(0, args.retries)),
        "--request-delay",
        str(max(0.0, args.request_delay)),
    ]

    if args.compiler:
        cmd.extend(["--compiler", args.compiler])

    return cmd


def main() -> int:
    args = parse_args()

    if not os.environ.get("AMAP_KEY", "").strip():
        print(
            "ERROR: AMAP_KEY environment variable is not set.\n"
            "On macOS, run: export AMAP_KEY=\"your-web-service-key\"",
            file=sys.stderr,
        )
        return 1

    try:
        output_dir = args.output_root / args.mode
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest_rows: list[tuple[str, object]] = [("mode", args.mode)]

        if args.mode == "smoke":
            points_path = args.smoke_points
            points = read_points(points_path)
            point_count = len(points)
            print("Stage        : SMOKE")
            print("Purpose      : verify compiler/API/key/order/basic compatibility")
            manifest_rows.extend(
                [
                    ("strategy", "fixed_smoke_points"),
                    ("points_file", str(points_path)),
                ]
            )
        elif args.full_strategy == "random":
            points_path = output_dir / "generated_random_points.csv"
            point_count, attempts = write_random_points(
                points_path,
                args.random_count,
                args.seed,
            )
            acceptance = point_count / attempts if attempts else 0.0
            print("Stage        : FULL")
            print("Strategy     : deterministic random / approximate area-uniform")
            print(f"Seed         : {args.seed}")
            print("Scope        : approximate GCJ distortion-scope polygon (PRCoords-derived)")
            print(f"Candidates   : {attempts}")
            print(f"Acceptance   : {acceptance * 100.0:.2f}%")
            print(f"Generated CSV: {points_path}")
            manifest_rows.extend(
                [
                    ("strategy", "random_area_uniform_rejection"),
                    ("random_count", point_count),
                    ("seed", args.seed),
                    ("candidate_attempts", attempts),
                    ("acceptance_ratio", f"{acceptance:.9f}"),
                    (
                        "scope",
                        "PRCoords approximate GCJ distortion scope; not an administrative boundary",
                    ),
                    ("points_file", str(points_path)),
                ]
            )
        else:
            anchors = read_points(args.full_anchors)
            points_path = output_dir / "generated_full_grid_points.csv"
            point_count = write_full_grid(
                anchors,
                points_path,
                args.grid_radius,
                args.grid_step_deg,
            )
            side = args.grid_radius * 2 + 1
            print("Stage        : FULL")
            print("Strategy     : anchor grids")
            print(f"Anchors      : {len(anchors)}")
            print(f"Grid/anchor  : {side} x {side}")
            print(f"Grid step    : {args.grid_step_deg:.6f} deg")
            print(f"Generated CSV: {points_path}")
            manifest_rows.extend(
                [
                    ("strategy", "anchor_grid"),
                    ("anchor_count", len(anchors)),
                    ("grid_side", side),
                    ("grid_step_deg", f"{args.grid_step_deg:.6f}"),
                    ("points_file", str(points_path)),
                ]
            )

        batches = math.ceil(point_count / AMAP_BATCH_SIZE)
        manifest_rows.extend(
            [
                ("point_count", point_count),
                ("amap_batch_size", AMAP_BATCH_SIZE),
                ("estimated_amap_requests", batches),
                ("max_error_m", args.max_error_m),
            ]
        )
        write_manifest(output_dir / "sampling_manifest.csv", manifest_rows)

        print(f"Points       : {point_count}")
        print(f"AMap batches : {batches} (max {AMAP_BATCH_SIZE} points/request)")
        print(f"Output dir   : {output_dir}")
        print()

        cmd = build_core_command(args, points_path, output_dir)
        proc = subprocess.run(cmd, check=False)
        return proc.returncode

    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
