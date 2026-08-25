# AMap validation for the C implementation

This repository contains a staged external validation workflow for the current C `wgs2gcj()` implementation.

It compares eviltransform with AMap's coordinate-conversion Web Service instead of only checking an internal `WGS84 -> GCJ-02 -> WGS84` round trip.

Only Python standard-library modules are used. The AMap key is read from the `AMAP_KEY` environment variable and is never stored in the repository or written to result files.

## Recommended two-stage workflow

Use:

```text
tests/run_amap_validation.py
```

Two modes are available:

- `smoke`: six fixed representative points; always run this first.
- `full`: large validation after smoke succeeds.

`smoke` uses `tests/validate_amap.py`. `full` uses `tests/validate_amap_large.py`, which keeps terminal output compact, reports progress, prints the worst points, and automatically retries AMap `10004 ACCESS_TOO_FREQUENT` rate-limit responses.

Both validators compile the repository's current `c/transform.c`, call its real `wgs2gcj()` implementation, query AMap using the exact same WGS84 coordinates, and compare the resulting GCJ-02 coordinates.

## Stage 1: smoke test

The smoke set contains:

```text
Beijing
Shanghai
Guangzhou
Chengdu
Urumqi
Haikou
```

On macOS:

```bash
export AMAP_KEY="your-web-service-key"
python3 tests/run_amap_validation.py --mode smoke
```

Only continue to full validation after smoke succeeds.

Output:

```text
tests/output/smoke/amap_compare_result.csv
tests/output/smoke/amap_compare_summary.csv
```

## Stage 2: 50,000-point random validation

The default full test is now:

```bash
python3 tests/run_amap_validation.py --mode full
```

It generates:

```text
50,000 deterministic random WGS84 points
```

Default seed:

```text
20260825
```

The sampling algorithm is deterministic, so the same seed generates the same coordinates. The generated coordinates are saved, making the exact test set auditable even if the sampling implementation changes later.

### Sampling method

The random test is designed for broad geographic coverage rather than concentration around cities:

1. longitude is sampled uniformly over the polygon bounding box;
2. latitude is sampled uniformly in `sin(latitude)`, approximating uniform surface-area sampling on a sphere;
3. rejection sampling keeps only points inside an approximate GCJ distortion-scope polygon;
4. coordinates are rounded to six decimal places before testing, matching AMap's coordinate-conversion input precision;
5. duplicate rounded coordinates are rejected.

The scope polygon is stored in:

```text
tests/gcj_scope.py
```

Its vertices are adapted from PRCoords' public-domain approximate GCJ distortion-scope data. It is used only to avoid obviously irrelevant samples and **must not be interpreted as an administrative or political boundary**.

Source:

```text
https://github.com/Artoria2e5/PRCoords
```

### API request count

AMap supports at most 40 coordinate pairs per coordinate-conversion request.

Therefore:

```text
50,000 / 40 = 1,250 requests
```

Check your own AMap account quota before running a large test. Current AMap documentation lists a 5,000 request/day coordinate-conversion quota for personal authenticated developers; quotas can depend on account status and service policy.

The wrapper prints the estimated request count before starting the API comparison.

## Reproduce or change the random test

Explicitly reproduce the default sample:

```bash
python3 tests/run_amap_validation.py \
  --mode full \
  --random-count 50000 \
  --seed 20260825
```

Smaller trial:

```bash
python3 tests/run_amap_validation.py --mode full --random-count 5000
```

Larger sample:

```bash
python3 tests/run_amap_validation.py --mode full --random-count 100000
```

Use a different deterministic sample:

```bash
python3 tests/run_amap_validation.py --mode full --seed 123456789
```

## Retained anchor-grid strategy

The older local-grid test remains available:

```bash
python3 tests/run_amap_validation.py --mode full --full-strategy grid
```

The default grid is 5 x 5 around each anchor in `tests/amap_test_points.csv`.

For example:

```bash
python3 tests/run_amap_validation.py \
  --mode full \
  --full-strategy grid \
  --grid-radius 3 \
  --grid-step-deg 0.02
```

## macOS requirements

- Python 3
- Internet access to `restapi.amap.com`
- AMap **Web Service API** key
- a C compiler

macOS normally exposes Apple Clang through `cc` or `clang` when Xcode Command Line Tools are installed.

Check:

```bash
clang --version
```

If necessary:

```bash
xcode-select --install
```

No third-party Python packages such as `requests`, `numpy`, or `pandas` are required.

If compiler auto-detection fails:

```bash
python3 tests/run_amap_validation.py --mode smoke --compiler clang
python3 tests/run_amap_validation.py --mode full --compiler clang
```

## AMap API handling

The validator follows AMap coordinate-conversion requirements:

- input order: `longitude,latitude`;
- `coordsys=gps` for GPS/WGS84 input;
- at most six digits after the decimal point;
- at most 40 coordinate pairs per request.

The exact same rounded WGS84 coordinate is given to eviltransform and AMap.

For large tests, terminal output is intentionally compact. The large validator reports batch progress and comparison progress rather than printing all 50,000 rows. If AMap returns `10004 ACCESS_TOO_FREQUENT`, the large validator backs off and retries within the configured retry budget.

## Full-mode output

Generated under:

```text
tests/output/full/
```

Files include:

```text
generated_random_points.csv
sampling_manifest.csv
amap_compare_result.csv
amap_compare_summary.csv
```

`generated_random_points.csv` contains the exact 50,000 WGS84 test points.

`sampling_manifest.csv` records items such as:

- sampling strategy;
- random count;
- seed;
- candidate/accepted count;
- estimated AMap request count.

`amap_compare_result.csv` contains every point's:

- WGS84 input;
- eviltransform GCJ-02 result;
- AMap GCJ-02 result;
- latitude/longitude difference;
- two-dimensional error in meters.

`amap_compare_summary.csv` contains:

- count;
- mean error;
- RMS;
- P95;
- P99;
- maximum error and worst point;
- AMap request attempts;
- rate-limit event count;
- validation PASS/FAIL;
- compiler;
- SHA-256 of the tested `c/transform.c`.

The large validator also prints the worst 20 points to the terminal.

## Threshold

Default engineering threshold:

```text
max error <= 5 m
```

This is a repository validation threshold, not an official AMap accuracy guarantee.

Change it:

```bash
python3 tests/run_amap_validation.py --mode full --max-error-m 10
```

Disable threshold-based failure while collecting data:

```bash
python3 tests/run_amap_validation.py --mode full --max-error-m 0
```

Exit codes:

- `0`: PASS, or threshold disabled;
- `1`: setup/compile/network/API error;
- `2`: comparison completed but maximum error exceeded the configured threshold.

## Direct validator use

Small/custom CSV:

```bash
python3 tests/validate_amap.py \
  --points my_points.csv \
  --output-dir tests/output/custom
```

Large/custom CSV:

```bash
python3 tests/validate_amap_large.py \
  --points my_points.csv \
  --output-dir tests/output/custom-large
```

Required CSV columns:

```csv
name,wgs_lat,wgs_lon
Beijing,39.908823,116.397470
```
