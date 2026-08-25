#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run staged AMap validation for eviltransform.

This is a thin wrapper around validate_amap.py.

Modes:
  smoke: Run a small fixed set of representative points first.
  full:  Expand the full anchor set into deterministic local grids and run a
         larger comparison only when explicitly requested.

Only Python standard-library modules are used.

Examples on macOS:

    export AMAP_KEY="your-web-service-key"

    # Fast sanity check first (default mode)
    python3 tests/run_amap_validation.py --mode smoke

    # Larger validation after smoke passes
    python3 tests/run_amap_validation.py --mode full

The full mode uses a 5 x 5 grid around each anchor by default. With the
repository's current 15 anchors this produces 375 points, which requires
10 AMap requests when the 40-points-per-request limit is used.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from pathlib import Path
import subprocess
import sys


DEFAULT_GRID_RADIUS = 2
DEFAULT_GRID_STEP_DEG = 0.05
AMAP_BATCH_SIZE = 40


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
        help="network retries per AMap request",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.05,
        help="delay between AMap request batches in seconds",
    )
    parser.add_argument(
        "--grid-radius",
        type=int,
        default=DEFAULT_GRID_RADIUS,
        help=(
            "full mode only: number of grid steps around each anchor; "
            "2 means a 5x5 grid"
        ),
    )
    parser.add_argument(
        "--grid-step-deg",
        type=float,
        default=DEFAULT_GRID_STEP_DEG,
        help="full mode only: latitude/longitude grid spacing in degrees",
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
        help="full-mode anchor CSV",
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

        if args.mode == "smoke":
            points_path = args.smoke_points
            points = read_points(points_path)
            point_count = len(points)
            print("Stage        : SMOKE")
            print("Purpose      : verify compiler/API/key/order/basic compatibility")
        else:
            anchors = read_points(args.full_anchors)
            points_path = output_dir / "generated_full_points.csv"
            point_count = write_full_grid(
                anchors,
                points_path,
                args.grid_radius,
                args.grid_step_deg,
            )
            side = args.grid_radius * 2 + 1
            print("Stage        : FULL")
            print(f"Anchors      : {len(anchors)}")
            print(f"Grid/anchor  : {side} x {side}")
            print(f"Grid step    : {args.grid_step_deg:.6f} deg")
            print(f"Generated CSV: {points_path}")

        batches = math.ceil(point_count / AMAP_BATCH_SIZE)
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
