# api/views_flood_risk.py
from __future__ import annotations

import math
from datetime import timezone as dt_timezone

from django.db import connection
from django.http import JsonResponse
from django.utils import timezone

from api import dem_utils


RISK_LEVELS = ["NONE", "LOW", "MEDIUM", "HIGH", "EXTREME"]


# =========================
# Helpers
# =========================
def _safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# =========================
# Effective rainfall
# =========================
def _effective_rain_1h(rain_1h: float) -> float:
    return max(0.0, _safe_float(rain_1h))


def _effective_rain_3h(rain_1h: float, rain_3h: float) -> float:
    r1 = max(0.0, _safe_float(rain_1h))
    r3 = max(0.0, _safe_float(rain_3h))
    extra_3h = max(0.0, r3 - r1)
    return max(0.0, r1 + 0.6 * extra_3h)


def _effective_rain_6h(rain_1h: float, rain_3h: float, rain_6h: float) -> float:
    r1 = max(0.0, _safe_float(rain_1h))
    r3 = max(0.0, _safe_float(rain_3h))
    r6 = max(0.0, _safe_float(rain_6h))
    extra_3h = max(0.0, r3 - r1)
    extra_6h = max(0.0, r6 - r3)
    return max(0.0, r1 + 0.6 * extra_3h + 0.4 * extra_6h)


# =========================
# Bands (0..4)
# =========================
def _rain_band_from_eff6(eff_6h: float) -> int:
    e = max(0.0, _safe_float(eff_6h))
    if e < 0.5:
        return 0
    if e < 5:
        return 1
    if e < 15:
        return 2
    if e < 30:
        return 3
    return 4


def _terrain_band_from_relief_local(relief_local_m: float) -> int:
    """
    relief_local = elev_center - local_min (>=0)
    Relief càng nhỏ => càng sát đáy vùng trũng => rủi ro ngập càng cao.

    Band 0..4:
      - >= 30m: ít trũng (0)
      - 10..30: (1)
      - 3..10 : (2)
      - 1..3  : (3)
      - < 1   : cực trũng (4)
    """
    r = max(0.0, _safe_float(relief_local_m, 1e9))
    if r >= 30:
        return 0
    if r >= 10:
        return 1
    if r >= 3:
        return 2
    if r >= 1:
        return 3
    return 4


def _elevation_band(elev_m: float) -> int:
    """
    Cao độ tuyệt đối thấp (đồng bằng ven biển) thường dễ ngập do thoát kém/triều/cửa sông.
    Đây là yếu tố phụ (không lấn át mưa).
    """
    e = _safe_float(elev_m, 99999.0)
    if e < 5:
        return 4
    if e < 20:
        return 3
    if e < 80:
        return 2
    if e < 200:
        return 1
    return 0


def _slope_like_penalty(slope_like_m: float) -> int:
    """
    slope_like: proxy độ dốc địa hình quanh điểm (m chênh trên bán kính nhỏ).
    Dốc lớn -> ít ngập ứ đọng (nhưng có thể lũ quét). Ở đây chỉ giảm nhẹ risk ngập tĩnh.
    """
    s = max(0.0, _safe_float(slope_like_m, 0.0))
    if s >= 80:
        return 2
    if s >= 30:
        return 1
    return 0


def _combined_risk_score(rain_band: int, relief_band: int, elev_band: int, slope_pen: int) -> int:
    """
    Kết hợp:
      - rain_band: chủ đạo (0.70)
      - relief_band (điểm trũng): quan trọng (0.20)
      - elev_band (độ cao tuyệt đối thấp): phụ (0.10)
      - slope_pen: giảm nhẹ 0..2

    Output 0..4
    """
    rain_band = int(_clamp(rain_band, 0, 4))
    relief_band = int(_clamp(relief_band, 0, 4))
    elev_band = int(_clamp(elev_band, 0, 4))
    slope_pen = int(_clamp(slope_pen, 0, 2))

    raw = 0.70 * rain_band + 0.20 * relief_band + 0.10 * elev_band
    score = int(round(raw))
    score = score - slope_pen
    return int(_clamp(score, 0, 4))


# =========================
# DEM sampling wrapper (never crash API)
# =========================
def _try_sample_dem(lat: float, lon: float):
    """
    Returns: (elev_m|None, relief_local_m|None, slope_like_m|None)
    slope_like: lấy 4 điểm xung quanh, đo max(|dz|) làm proxy dốc.
    """
    try:
        elev = dem_utils.sample_elevation(lat, lon)
        relief = dem_utils.sample_relief_local(lat, lon, half_size_px=15)

        # Nếu không có elev thì không tính slope_like
        if elev is None:
            return None, relief, None

        # Lấy offset nhỏ theo độ (xấp xỉ), đủ dùng làm proxy
        # 0.01 deg ~ 1.1km theo vĩ độ (gần VN)
        d = 0.01
        e_n = dem_utils.sample_elevation(lat + d, lon)
        e_s = dem_utils.sample_elevation(lat - d, lon)
        e_e = dem_utils.sample_elevation(lat, lon + d)
        e_w = dem_utils.sample_elevation(lat, lon - d)

        diffs = []
        for ex in (e_n, e_s, e_e, e_w):
            if ex is None:
                continue
            diffs.append(abs(float(ex) - float(elev)))

        slope_like = max(diffs) if diffs else None
        return elev, relief, slope_like
    except Exception:
        # Thiếu rasterio/pyproj, thiếu file DEM, lỗi CRS... => bỏ terrain
        return None, None, None


# =========================
# View
# =========================
def flood_risk_latest(request):
    now_utc = timezone.now().astimezone(dt_timezone.utc)
    now_hour = now_utc.replace(minute=0, second=0, microsecond=0)

    with connection.cursor() as cur:
        cur.execute(
            """
            WITH
            params AS (
              SELECT %s::timestamptz AS t
            ),
            hours AS (
              SELECT generate_series(
                (SELECT t FROM params) - interval '6 hour',
                (SELECT t FROM params),
                interval '1 hour'
              ) AS valid_at
            ),
            grid AS (
              SELECT id, lat, lon
              FROM public.locations
              WHERE active = true
            ),
            merged AS (
              SELECT
                g.id AS location_id,
                g.lat, g.lon,
                h.valid_at,
                GREATEST(COALESCE(o.precip_mm, f.precip_mm, 0.0), 0.0) AS precip_mm
              FROM grid g
              CROSS JOIN hours h
              LEFT JOIN public.weather_hourly_obs o
                ON o.location_id = g.id
               AND o.source = 'openmeteo'
               AND o.valid_at = h.valid_at
              LEFT JOIN public.weather_hourly_fcst f
                ON f.location_id = g.id
               AND f.valid_at = h.valid_at
            ),
            agg AS (
              SELECT
                m.location_id, m.lat, m.lon,
                (SELECT t FROM params) AS valid_at,
                SUM(CASE WHEN (SELECT t FROM params) - m.valid_at <= interval '1 hour' THEN m.precip_mm ELSE 0 END) AS rain_1h_mm,
                SUM(CASE WHEN (SELECT t FROM params) - m.valid_at <= interval '3 hour' THEN m.precip_mm ELSE 0 END) AS rain_3h_mm,
                SUM(CASE WHEN (SELECT t FROM params) - m.valid_at <= interval '6 hour' THEN m.precip_mm ELSE 0 END) AS rain_6h_mm
              FROM merged m
              GROUP BY m.location_id, m.lat, m.lon
            )
            SELECT
              location_id, lat, lon, valid_at,
              rain_1h_mm, rain_3h_mm, rain_6h_mm
            FROM agg
            ORDER BY lat, lon;
            """,
            [now_hour],
        )
        rows = cur.fetchall()

    out = []
    for (
        location_id, lat, lon, valid_at,
        rain_1h_mm, rain_3h_mm, rain_6h_mm
    ) in rows:
        r1 = _safe_float(rain_1h_mm, 0.0)
        r3 = _safe_float(rain_3h_mm, 0.0)
        r6 = _safe_float(rain_6h_mm, 0.0)

        eff1 = _effective_rain_1h(r1)
        eff3 = _effective_rain_3h(r1, r3)
        eff6 = _effective_rain_6h(r1, r3, r6)

        rain_band = _rain_band_from_eff6(eff6)

        # DEM features (no DB required)
        elev_m, relief_local_m, slope_like_m = _try_sample_dem(float(lat), float(lon))

        if relief_local_m is None:
            relief_band = 0
        else:
            relief_band = _terrain_band_from_relief_local(relief_local_m)

        if elev_m is None:
            elev_band = 0
        else:
            elev_band = _elevation_band(elev_m)

        slope_pen = _slope_like_penalty(slope_like_m) if slope_like_m is not None else 0

        score = _combined_risk_score(rain_band, relief_band, elev_band, slope_pen)
        level = RISK_LEVELS[int(score)]

        # IMPORTANT: giữ output giống format cũ để FE không đổi
        out.append({
            "location_id": str(location_id),
            "lat": float(lat),
            "lon": float(lon),
            "valid_at": valid_at.isoformat(),

            # Giữ tên relief_m như cũ, nhưng giá trị là relief_local (m)
            "relief_m": None if relief_local_m is None else float(relief_local_m),

            "rain_1h_mm": float(r1),
            "rain_3h_mm": float(r3),
            "eff_rain_1h_mm": float(eff1),
            "eff_rain_3h_mm": float(eff3),

            "risk_score": int(score),
            "risk_level": level,
        })

    return JsonResponse({"count": len(out), "data": out})
