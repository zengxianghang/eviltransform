#include <math.h>
#include <stdlib.h>

#include "transform.h"

/*
 * Use inline when the compiler supports C99.
 * The original implementation keeps these tiny helper functions inline
 * because they are called frequently during coordinate conversion.
 */
#undef INLINE
#if defined(__STDC_VERSION__) && __STDC_VERSION__ >= 199900L
#define INLINE inline
#else
#define INLINE
#endif /* STDC */

/*
 * Custom fabs implementation.
 * Avoid calling the standard library function repeatedly in performance
 * sensitive paths.
 */
#define fabs(x) __ev_fabs(x)

/*
 * Return absolute value of a double.
 * The implementation intentionally avoids comparisons such as >= to keep
 * optimization opportunities for some compilers.
 */
INLINE static double __ev_fabs(double x){ return x > 0.0 ? x : -x; }

/*
 * Check whether a coordinate is outside the GCJ-02 transformation area.
 *
 * GCJ-02 is designed for mainland China only. Coordinates outside this
 * approximate bounding box are returned without transformation.
 */
INLINE static int outOfChina(double lat, double lng) {
	if (lng < 72.004 || lng > 137.8347) {
		return 1;
	}
	if (lat < 0.8293 || lat > 55.8271) {
		return 1;
	}
	return 0;
}

/*
 * Earth radius parameter used by the GCJ-02 conversion model.
 */
#define EARTH_R 6378137.0

/*
 * Calculate the raw latitude and longitude offset.
 *
 * Input:
 *   x = longitude offset from reference longitude (lng - 105)
 *   y = latitude offset from reference latitude (lat - 35)
 *
 * Output:
 *   lat = raw latitude perturbation
 *   lng = raw longitude perturbation
 *
 * The GCJ-02 algorithm uses a combination of polynomial terms and periodic
 * sine functions to generate a smooth nonlinear offset field.
 */
void transform(double x, double y, double *lat, double *lng) {
	double xy = x * y;
	double absX = sqrt(fabs(x));
	double xPi = x * M_PI;
	double yPi = y * M_PI;
	double d = 20.0*sin(6.0*xPi) + 20.0*sin(2.0*xPi);

	*lat = d;
	*lng = d;

	/* Periodic latitude and longitude components. */
	*lat += 20.0*sin(yPi) + 40.0*sin(yPi/3.0);
	*lng += 20.0*sin(xPi) + 40.0*sin(xPi/3.0);

	/* Long wavelength periodic components. */
	*lat += 160.0*sin(yPi/12.0) + 320*sin(yPi/30.0);
	*lng += 150.0*sin(xPi/12.0) + 300.0*sin(xPi/30.0);

	/* Scale periodic components. */
	*lat *= 2.0 / 3.0;
	*lng *= 2.0 / 3.0;

	/* Polynomial correction terms. */
	*lat += -100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*xy + 0.2*absX;
	*lng += 300.0 + x + 2.0*y + 0.1*x*x + 0.1*xy + 0.1*absX;
}

/*
 * Calculate GCJ-02 offset in degrees.
 *
 * The transform() result is not directly a latitude/longitude offset.
 * This function converts the raw perturbation into angular displacement
 * according to the ellipsoid model.
 */
static void delta(double lat, double lng, double *dLat, double *dLng) {
	if ((dLat == NULL) || (dLng == NULL)) {
		return;
	}

	/* First eccentricity squared of the reference ellipsoid. */
	const double ee = 0.00669342162296594323;

	/* Generate raw perturbation around (105E, 35N). */
	transform(lng-105.0, lat-35.0, dLat, dLng);

	double radLat = lat / 180.0 * M_PI;
	double magic = sin(radLat);
	magic = 1 - ee*magic*magic;
	double sqrtMagic = sqrt(magic);

	/* Convert meters-like perturbation into latitude degrees. */
	*dLat = (*dLat * 180.0) /
		((EARTH_R * (1 - ee)) / (magic * sqrtMagic) * M_PI);

	/* Convert meters-like perturbation into longitude degrees. */
	*dLng = (*dLng * 180.0) /
		(EARTH_R / sqrtMagic * cos(radLat) * M_PI);
}

/*
 * Convert WGS-84 coordinates to GCJ-02 coordinates.
 */
void wgs2gcj(double wgsLat, double wgsLng, double *gcjLat, double *gcjLng) {
	if ((gcjLat == NULL) || (gcjLng == NULL)) {
		return;
	}

	/* Do not transform coordinates outside mainland China. */
	if (outOfChina(wgsLat, wgsLng)) {
		*gcjLat = wgsLat;
		*gcjLng = wgsLng;
		return;
	}

	double dLat, dLng;
	delta(wgsLat, wgsLng, &dLat, &dLng);

	*gcjLat = wgsLat + dLat;
	*gcjLng = wgsLng + dLng;
}

/*
 * Fast approximate conversion from GCJ-02 to WGS-84.
 *
 * This method assumes that the local offset field does not change much
 * within several hundred meters:
 *
 *     WGS ~= GCJ - delta(GCJ)
 *
 * It is fast but normally has meter-level residual error.
 */
void gcj2wgs(double gcjLat, double gcjLng, double *wgsLat, double *wgsLng) {
	if ((wgsLat == NULL) || (wgsLng == NULL)) {
		return;
	}

	if (outOfChina(gcjLat, gcjLng)) {
		*wgsLat = gcjLat;
		*wgsLng = gcjLng;
		return;
	}

	double dLat, dLng;
	delta(gcjLat, gcjLng, &dLat, &dLng);

	*wgsLat = gcjLat - dLat;
	*wgsLng = gcjLng - dLng;
}

/*
 * High accuracy GCJ-02 to WGS-84 conversion.
 *
 * Uses binary search instead of direct subtraction.
 * Each iteration:
 *   1. Select a WGS-84 candidate.
 *   2. Convert it back to GCJ-02.
 *   3. Compare with target GCJ-02 coordinate.
 *   4. Shrink the search interval.
 */
void gcj2wgs_exact(double gcjLat, double gcjLng, double *wgsLat, double *wgsLng) {
	double initDelta = 0.01;
	double threshold = 0.000001;
	double dLat, dLng, mLat, mLng, pLat, pLng;

	/* Initial search window: approximately +/- 1 km. */
	dLat = dLng = initDelta;
	mLat = gcjLat - dLat;
	mLng = gcjLng - dLng;
	pLat = gcjLat + dLat;
	pLng = gcjLng + dLng;

	int i;
	for (i=0; i<30; i++) {
		/* Candidate WGS-84 coordinate. */
		*wgsLat = (mLat+pLat) / 2;
		*wgsLng = (mLng+pLng) / 2;

		double tmpLat, tmpLng;

		/* Forward conversion is used as the error function. */
		wgs2gcj(*wgsLat, *wgsLng, &tmpLat, &tmpLng);

		dLat = tmpLat - gcjLat;
		dLng = tmpLng - gcjLng;

		/* Stop when the GCJ-02 residual is sufficiently small. */
		if ((fabs(dLat)<threshold) && (fabs(dLng)<threshold)) {
			return;
		}

		/* Update latitude search interval. */
		if (dLat > 0) {
			pLat = *wgsLat;
		} else {
			mLat = *wgsLat;
		}

		/* Update longitude search interval. */
		if (dLng > 0) {
			pLng = *wgsLng;
		} else {
			mLng = *wgsLng;
		}
	}
}

/*
 * Calculate spherical distance between two coordinates.
 *
 * The result is returned in meters using the earth radius defined above.
 */
double distance(double latA, double lngA, double latB, double lngB) {
	double arcLatA = latA * M_PI/180;
	double arcLatB = latB * M_PI/180;

	/* Spherical law of cosines. */
	double x = cos(arcLatA) * cos(arcLatB) * cos((lngA-lngB)*M_PI/180);
	double y = sin(arcLatA) * sin(arcLatB);
	double s = x + y;

	/* Clamp because floating point errors may exceed [-1,1]. */
	if (s > 1) {
		s = 1;
	}
	if (s < -1) {
		s = -1;
	}

	double alpha = acos(s);
	return alpha * EARTH_R;
}
