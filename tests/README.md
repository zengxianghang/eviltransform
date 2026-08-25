# AMap validation for the C implementation

`validate_amap.py` compares the repository's current C `wgs2gcj()` implementation with AMap's coordinate conversion Web Service.

The validator uses only the Python standard library. It does **not** contain an AMap key and does not write the key to output files.

## What it validates

For every WGS84 test point:

1. The script compiles the current `c/transform.c` with a temporary C harness.
2. The compiled program calls `wgs2gcj()`.
3. The same WGS84 coordinate is sent to AMap with `coordsys=gps`.
4. The two GCJ-02 coordinates are compared in meters.
5. Detailed and summary CSV reports are generated.

This avoids the weak test pattern of only checking `WGS84 -> GCJ-02 -> WGS84`, which can prove internal consistency but cannot prove compatibility with an external GCJ-02 implementation.

## AMap API requirements

Use a **Web Service API** key.

Official coordinate-conversion documentation:

https://lbs.amap.com/api/webservice/guide/api/convert

The AMap Web Service documentation specifies that:

- input order is `longitude,latitude`;
- `coordsys=gps` is used for GPS/WGS84 input;
- input coordinates may contain at most 6 digits after the decimal point;
- one request supports at most 40 coordinate pairs.

The validator therefore rounds each input point to 6 decimal places and uses the exact same rounded coordinate for both eviltransform and AMap. Requests are batched with at most 40 points.

## Requirements

- Python 3
- Internet access to `restapi.amap.com`
- A C compiler:
  - `gcc`, `clang`, or `cc`; or
  - MSVC `cl` from a Visual Studio Developer Command Prompt
- an AMap Web Service API key

No third-party Python packages are required.

## Run on Windows PowerShell

```powershell
$env:AMAP_KEY="your-web-service-key"
python tests/validate_amap.py
```

## Run on cmd.exe

```bat
set AMAP_KEY=your-web-service-key
python tests\validate_amap.py
```

If the compiler is not auto-detected:

```bat
python tests\validate_amap.py --compiler gcc
```

or run from a Visual Studio Developer Command Prompt so that `cl.exe` is on `PATH`.

## Input CSV

Default input:

```text
tests/amap_test_points.csv
```

Required columns:

```csv
name,wgs_lat,wgs_lon
Beijing,39.908823,116.397470
```

Additional points can be added. The coordinates are treated as WGS84 inputs; they do not need to be survey control points because the test compares two transformations of the same input coordinate.

## Output

Generated under:

```text
tests/output/
```

Detailed result:

```text
amap_compare_result.csv
```

Columns include:

- WGS84 input;
- eviltransform GCJ-02 result;
- AMap GCJ-02 result;
- latitude/longitude difference in degrees;
- two-dimensional distance error in meters.

Summary:

```text
amap_compare_summary.csv
```

Statistics include:

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

This is a repository validation threshold, **not an official accuracy guarantee from AMap**.

Change it with:

```bat
python tests\validate_amap.py --max-error-m 2
```

Disable threshold-based failure while collecting data:

```bat
python tests\validate_amap.py --max-error-m 0
```

Exit codes:

- `0`: PASS, or threshold disabled;
- `1`: setup/compile/network/API error;
- `2`: comparison completed but maximum error exceeded the configured threshold.
