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

`smoke` uses `tests/validate_amap.py`. `full` uses `tests/validate_amap_large.py`, which keeps terminal output compact, reports progress, retries AMap rate limits, and persistently caches successful AMap results so an interrupted large run can resume.

Both validators compile the repository's current `c/transform.c`, call its real `wgs2gcj()` implementation, query AMap using the exact same WGS84 coordinates, and compare the resulting GCJ-02 coordinates.

## Stage 1: smoke test

The smoke set contains Beijing, Shanghai, Guangzhou, Chengdu, Urumqi, and Haikou.

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

The default full test is:

```bash
python3 tests/run_amap_validation.py --mode full
```

It generates 50,000 deterministic random WGS84 points. The default seed is:

```text
20260825
```

The same seed generates the same coordinates. The exact generated set is saved for auditing and reproducibility.

### Sampling method

The random test is designed for broad geographic coverage rather than concentration around cities:

1. longitude is sampled uniformly over the scope bounding box;
2. latitude is sampled uniformly in `sin(latitude)`, approximating uniform surface-area sampling on a sphere;
3. rejection sampling keeps only points inside an approximate GCJ distortion-scope polygon;
4. coordinates are rounded to six decimal places before testing;
5. duplicate rounded coordinates are rejected.

The scope polygon is stored in `tests/gcj_scope.py`. Its vertices are adapted from PRCoords' approximate GCJ distortion-scope data. It is used only to avoid obviously irrelevant samples and must not be interpreted as an administrative or political boundary.

Source:

```text
https://github.com/Artoria2e5/PRCoords
```

### API request count

AMap supports at most 40 coordinate pairs per coordinate-conversion request, so a fresh 50,000-point run needs approximately:

```text
50,000 / 40 = 1,250 requests
```

The wrapper prints the estimated fresh-run request count before starting. A resumed run normally uses fewer requests because already cached coordinates are skipped.

## Resumable full validation and AMap cache

Large validation uses a persistent cache by default:

```text
tests/output/full/amap_reference_cache.csv
```

The cache key is the normalized six-decimal WGS84 coordinate, not the generated point name. Each successful AMap batch is appended to the cache and immediately persisted with `flush` + `fsync` before the next network request.

Therefore, if a 50,000-point run stops because of network failure, rate limiting, terminal interruption, or machine restart, simply run the same command again:

```bash
python3 tests/run_amap_validation.py --mode full --max-error-m 0
```

The large validator will print values such as:

```text
Cache hits   : 32000/50000
Need fetch   : 18000 point(s)
```

and will request only the missing coordinates. Successfully cached points do not consume another AMap request on that run.

The detailed comparison CSV is generated after all required AMap reference coordinates are available. The persistent cache is the checkpoint used for resume.

### Force a fresh AMap reference set

Normally the cache should be retained. To force a complete refresh, either delete it:

```bash
rm tests/output/full/amap_reference_cache.csv
```

or call the large validator directly with `--refresh-cache`:

```bash
python3 tests/validate_amap_large.py \
  --points tests/output/full/generated_random_points.csv \
  --output-dir tests/output/full \
  --refresh-cache \
  --max-error-m 0
```

Refreshing is useful if you intentionally want to re-query AMap rather than compare against previously frozen reference responses.

A custom cache location is also supported by the large validator with `--cache-file`.

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

Use a different deterministic sample:

```bash
python3 tests/run_amap_validation.py --mode full --seed 123456789
```

Because the cache is coordinate-keyed, any coordinates shared with an earlier sample are reused automatically.

## Retained anchor-grid strategy

The older local-grid test remains available:

```bash
python3 tests/run_amap_validation.py --mode full --full-strategy grid
```

The default grid is 5 x 5 around each anchor in `tests/amap_test_points.csv`.

## macOS requirements

- Python 3
- Internet access to `restapi.amap.com`
- AMap Web Service API key
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

## AMap API handling

The validator follows these coordinate-conversion conventions used by this repository:

- input order: `longitude,latitude`;
- `coordsys=gps` for GPS/WGS84 input;
- coordinates normalized to six decimal places;
- at most 40 coordinate pairs per request.

The exact same rounded WGS84 coordinate is given to eviltransform and AMap.

For large tests, the default inter-request delay is 0.50 seconds. The large validator automatically backs off and retries AMap minute-window and QPS rate-limit responses, including `10021 CUQPS_HAS_EXCEEDED_THE_LIMIT`, within the configured retry budget.

## Full-mode output

Generated under:

```text
tests/output/full/
```

Files include:

```text
generated_random_points.csv
sampling_manifest.csv
amap_reference_cache.csv
amap_compare_result.csv
amap_compare_summary.csv
```

`generated_random_points.csv` contains the exact WGS84 test points.

`sampling_manifest.csv` records the sampling strategy, random count, seed, accepted/candidate count, and estimated fresh-run AMap request count.

`amap_reference_cache.csv` is the persistent checkpoint. It stores normalized WGS84 coordinates and the corresponding AMap GCJ-02 outputs.

`amap_compare_result.csv` contains every point's WGS84 input, eviltransform GCJ-02 result, AMap GCJ-02 result, latitude/longitude differences, and distance error in meters.

`amap_compare_summary.csv` contains count, mean, RMS, P95, P99, maximum error, cache hits, points fetched during the current run, request attempts, rate-limit events, validation result, compiler, and SHA-256 of the tested `c/transform.c`.

The large validator also prints the worst 20 points to the terminal.

## Threshold

The default engineering threshold is:

```text
max error <= 5 m
```

This is a repository validation threshold, not an official AMap accuracy guarantee.

For initial 50,000-point data collection, it is usually better to disable threshold-based failure:

```bash
python3 tests/run_amap_validation.py --mode full --max-error-m 0
```

After observing the actual distribution, choose a regression threshold based on the measured behavior.

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
