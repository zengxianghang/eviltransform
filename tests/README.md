# AMap validation for the C implementation

The repository contains a staged external validation workflow for the current C `wgs2gcj()` implementation.

It compares eviltransform with AMap's coordinate-conversion Web Service instead of only checking an internal `WGS84 -> GCJ-02 -> WGS84` round trip.

Only Python standard-library modules are used. The AMap key is read from the `AMAP_KEY` environment variable and is never stored in the repository or written to result files.

## Recommended two-stage workflow

Use the staged entry point:

```text
tests/run_amap_validation.py
```

Two modes are available:

- `smoke`: small, fast sanity check. This is the default and should always be run first.
- `full`: larger comparison after the smoke test passes.

The underlying implementation is still performed by:

```text
tests/validate_amap.py
```

That script compiles the repository's current `c/transform.c`, runs its real `wgs2gcj()` function, calls AMap for the same WGS84 coordinates, and compares the resulting GCJ-02 coordinates.

## Why smoke first?

The smoke test is intended to catch basic failures before consuming more API calls:

- invalid or unavailable AMap Web Service key;
- no working C compiler;
- longitude/latitude order problems;
- network/API failures;
- a clearly incompatible GCJ-02 implementation.

The smoke set currently contains six geographically distributed points:

```text
Beijing
Shanghai
Guangzhou
Chengdu
Urumqi
Haikou
```

If these points show tens or hundreds of meters of difference, stop and investigate before running the full comparison.

## Full validation size

The full-mode anchor file is:

```text
tests/amap_test_points.csv
```

By default, `run_amap_validation.py --mode full` generates a deterministic 5 x 5 local grid around every anchor:

```text
grid radius = 2
grid step   = 0.05 degree
```

With the current 15 anchors:

```text
15 anchors x 25 points = 375 test points
```

AMap supports at most 40 coordinate pairs per conversion request, so the default full test requires 10 requests.

The generated full input is saved as:

```text
tests/output/full/generated_full_points.csv
```

You can increase or decrease the coverage with `--grid-radius` and `--grid-step-deg`.

## macOS requirements

- Python 3
- Internet access to `restapi.amap.com`
- AMap **Web Service API** key
- a C compiler

macOS normally already exposes Apple Clang through `cc` or `clang` when Xcode Command Line Tools are installed.

Check:

```bash
clang --version
```

If necessary:

```bash
xcode-select --install
```

No third-party Python packages such as `requests`, `numpy`, or `pandas` are required.

## Run on macOS

Set the AMap key for the current shell:

```bash
export AMAP_KEY="your-web-service-key"
```

### Stage 1: smoke test

```bash
python3 tests/run_amap_validation.py --mode smoke
```

`--mode smoke` is also the default, so this is equivalent to:

```bash
python3 tests/run_amap_validation.py
```

Expected output directory:

```text
tests/output/smoke/
```

Only continue to full validation after the smoke test succeeds.

### Stage 2: full validation

```bash
python3 tests/run_amap_validation.py --mode full
```

Expected output directory:

```text
tests/output/full/
```

If compiler auto-detection fails, explicitly select Apple Clang:

```bash
python3 tests/run_amap_validation.py --mode smoke --compiler clang
```

and then:

```bash
python3 tests/run_amap_validation.py --mode full --compiler clang
```

## Adjust the full test size

Default:

```bash
python3 tests/run_amap_validation.py --mode full
```

uses a 5 x 5 grid around each anchor.

Smaller full test, 3 x 3 per anchor:

```bash
python3 tests/run_amap_validation.py --mode full --grid-radius 1
```

Larger test, 7 x 7 per anchor:

```bash
python3 tests/run_amap_validation.py --mode full --grid-radius 3
```

Change local spacing:

```bash
python3 tests/run_amap_validation.py --mode full --grid-step-deg 0.02
```

For the current 15 anchors:

- radius `1`: 135 points;
- radius `2`: 375 points;
- radius `3`: 735 points.

## AMap API handling

The validator follows the AMap coordinate-conversion interface conventions used by this repository:

- input order: `longitude,latitude`;
- `coordsys=gps` for GPS/WGS84 input;
- coordinates normalized to 6 decimal places;
- at most 40 coordinate pairs per request.

The exact same rounded WGS84 coordinate is sent to both eviltransform and AMap so the comparison does not mix input-precision differences with transformation differences.

## Output files

Each mode writes its own files and does not overwrite the other mode.

Smoke:

```text
tests/output/smoke/amap_compare_result.csv
tests/output/smoke/amap_compare_summary.csv
```

Full:

```text
tests/output/full/generated_full_points.csv
tests/output/full/amap_compare_result.csv
tests/output/full/amap_compare_summary.csv
```

The detailed CSV contains:

- WGS84 input;
- eviltransform GCJ-02 result;
- AMap GCJ-02 result;
- latitude/longitude difference in degrees;
- two-dimensional difference in meters.

The summary contains:

- count;
- mean error;
- RMS error;
- P95;
- P99;
- maximum error and worst point;
- validation PASS/FAIL;
- compiler;
- SHA-256 of the tested `c/transform.c`.

## Threshold

The default engineering threshold is:

```text
max error <= 5 m
```

This is a repository validation threshold, not an official AMap accuracy guarantee.

Change it with:

```bash
python3 tests/run_amap_validation.py --mode smoke --max-error-m 2
```

or:

```bash
python3 tests/run_amap_validation.py --mode full --max-error-m 2
```

Disable threshold-based failure while collecting data:

```bash
python3 tests/run_amap_validation.py --mode full --max-error-m 0
```

Exit codes propagated from the core validator:

- `0`: PASS, or threshold disabled;
- `1`: setup/compile/network/API error;
- `2`: comparison completed but maximum error exceeded the configured threshold.

## Direct core-validator use

For custom CSV input, `validate_amap.py` can still be called directly:

```bash
python3 tests/validate_amap.py --points my_points.csv --output-dir tests/output/custom
```

Required CSV columns:

```csv
name,wgs_lat,wgs_lon
Beijing,39.908823,116.397470
```
