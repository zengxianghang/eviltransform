#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run staged AMap validation for eviltransform.

macOS examples:

    export AMAP_KEY="your-web-service-key"

    # Stage 1: six fixed smoke points
    python3 tests/run_amap_validation.py --mode smoke

    # Stage 2: 50,000 deterministic nationwide-style random points
    python3 tests/run_amap_validation.py --mode full

Full mode defaults to deterministic random sampling. The older anchor-grid
strategy is retained with --full-strategy grid.
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
DEFAULT_REQUEST_DELAY_SEC = 0.50
DEFAULT_RETRIES = 5
AMAP_BATCH_SIZE = 40
MASK64 = (1 << 64) - 1


class SplitMix64:
    """Fixed PRNG for cross-run reproducible validation coordinates."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK64

    def next_u64(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return (z ^ (z >> 31)) & MASK64

    def random(self) -> float:
        return (self.next_u64() >> 11) * (1.0 / (1 << 53))


def parse_args() -> argparse.Namespace:
    tests_dir = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(
        description="Run smoke or full AMap validation for eviltransform C."
    )
    p.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    p.add_argument(
        "--full-strategy", choices=("random", "grid"), default="random"
    )
    p.add_argument("--random-count", type=int, default=DEFAULT_RANDOM_COUNT)
    p.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    p.add_argument("--compiler", default=None)
    p.add_argument("--max-error-m", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument(
        "--request-delay",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SEC,
        help=(
            "delay between AMap requests in seconds; default 0.50 keeps the "
            "request rate comfortably below the documented 3 QPS personal "
            "coordinate-conversion limit"
        ),
    )
    p.add_argument("--grid-radius", type=int, default=DEFAULT_GRID_RADIUS)
    p.add_argument("--grid-step-deg", type=float, default=DEFAULT_GRID_STEP_DEG)
    p.add_argument(
        "--smoke-points",
        type=Path,
        default=tests_dir / "amap_smoke_points.csv",
    )
    p.add_argument(
        "--full-anchors",
        type=Path,
        default=tests_dir / "amap_test_points.csv",
    )
    p.add_argument(
        "--output-root", type=Path, default=tests_dir / "output"
    )
    return p.parse_args()


def read_points(path: Path) -> list[tuple[str, float, float]]:
    if not path.is_file():
        raise FileNotFoundError(f"point file not found: {path}")

    required = {"name", "wgs_lat", "wgs_lon"}
    points: list[tuple[str, float, float]] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
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
    """Generate deterministic points approximately uniformly by surface area."""
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
                    f"unable to generate {count} in-scope points after {attempts} attempts"
                )

            lon = min_lon + (max_lon - min_lon) * rng.random()
            sin_lat = sin_min + (sin_max - sin_min) * rng.random()
            lat = math.degrees(math.asin(sin_lat))
            lat = round(lat, 6)
            lon = round(lon, 6)

            if not contains_gcj_scope(lat, lon):
                continue
            key = (lat, lon)
            if key in seen:
                continue

            seen.add(key)
            writer.writerow(
                [
                    f"Random{len(seen):05d}",
                    f"{lat:.6f}",
                    f"{lon:.6f}",
                ]
            )

    return len(seen), attempts


def write_grid(
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
                    writer.writerow(
                        [
                            f"{anchor_name}_dy{iy:+d}_dx{ix:+d}",
                            f"{lat:.6f}",
                            f"{lon:.6f}",
                        ]
                    )
                    count += 1
    return count


def write_manifest(path: Path, rows: list[tuple[str, object]]) -> None:
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
    core_name = "validate_amap_large.py" if args.mode == "full" else "validate_amap.py"
    core = tests_dir / core_name
    if not core.is_file():
        raise FileNotFoundError(f"validator not found: {core}")

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
            "ERROR: AMAP_KEY is not set. On macOS run:\n"
            "export AMAP_KEY=\"your-web-service-key\"",
            file=sys.stderr,
        )
        return 1

    try:
        output_dir = args.output_root / args.mode
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest: list[tuple[str, object]] = [("mode", args.mode)]

        if args.mode == "smoke":
            points_path = args.smoke_points
            point_count = len(read_points(points_path))
            print("Stage        : SMOKE")
            print("Purpose      : compiler/API/key/order/basic compatibility")
            manifest += [
                ("strategy", "fixed_smoke_points"),
                ("points_file", str(points_path)),
            ]

        elif args.full_strategy == "random":
            points_path = output_dir / "generated_random_points.csv"
            point_count, attempts = write_random_points(
                points_path, args.random_count, args.seed
            )
            acceptance = point_count / attempts
            print("Stage        : FULL")
            print("Strategy     : deterministic random / approximate area-uniform")
            print(f"Seed         : {args.seed}")
            print("Scope        : PRCoords-derived approximate GCJ distortion scope")
            print(f"Candidates   : {attempts}")
            print(f"Acceptance   : {acceptance * 100.0:.2f}%")
            print(f"Generated CSV: {points_path}")
            manifest += [
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

        else:
            anchors = read_points(args.full_anchors)
            points_path = output_dir / "generated_full_grid_points.csv"
            point_count = write_grid(
                anchors, points_path, args.grid_radius, args.grid_step_deg
            )
            side = args.grid_radius * 2 + 1
            print("Stage        : FULL")
            print("Strategy     : anchor grids")
            print(f"Anchors      : {len(anchors)}")
            print(f"Grid/anchor  : {side} x {side}")
            print(f"Generated CSV: {points_path}")
            manifest += [
                ("strategy", "anchor_grid"),
                ("anchor_count", len(anchors)),
                ("grid_side", side),
                ("grid_step_deg", f"{args.grid_step_deg:.6f}"),
                ("points_file", str(points_path)),
            ]

        batches = math.ceil(point_count / AMAP_BATCH_SIZE)
        manifest += [
            ("point_count", point_count),
            ("amap_batch_size", AMAP_BATCH_SIZE),
            ("estimated_amap_requests", batches),
            ("request_delay_sec", f"{max(0.0, args.request_delay):.3f}"),
            ("retries", max(0, args.retries)),
            ("max_error_m", args.max_error_m),
        ]
        write_manifest(output_dir / "sampling_manifest.csv", manifest)

        print(f"Points       : {point_count}")
        print(f"AMap batches : {batches} (max {AMAP_BATCH_SIZE} points/request)")
        print(f"Request delay: {max(0.0, args.request_delay):.3f} s")
        print(f"Output dir   : {output_dir}")
        print()

        return subprocess.run(
            build_core_command(args, points_path, output_dir), check=False
        ).returncode

    except (OSError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())