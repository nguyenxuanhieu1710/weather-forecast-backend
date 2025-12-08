# api/views_flood.py
import math
from django.http import JsonResponse
from django.db import connection

from .dem_utils import sample_relief_local

EARTH_RADIUS_KM = 6371.0


def _haversine_km(lat1, lon1, lat2, lon2):
    """
    Khoảng cách great-circle giữa 2 điểm (độ) → km
    """
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad

    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


def _add_neighborhood_rain(points, radius_km=20.0, neighbor_weight=0.5):
    """
    Tính thêm lượng mưa "hiệu dụng" có xét ảnh hưởng lân cận.

    points: list[dict] với các keys:
      - lat, lon
      - rain_1h_mm, rain_3h_mm

    Bổ sung:
      - eff_rain_1h_mm
      - eff_rain_3h_mm
    """
    n = len(points)
    if n == 0:
        return

    for i in range(n):
        pi = points[i]
        lat_i = pi["lat"]
        lon_i = pi["lon"]

        sum_w = 0.0
        sum_r1 = 0.0
        sum_r3 = 0.0

        for j in range(n):
            if i == j:
                continue
            pj = points[j]
            d_km = _haversine_km(lat_i, lon_i, pj["lat"], pj["lon"])
            if d_km <= 0.0 or d_km > radius_km:
                continue

            w = 1.0 / (d_km + 1e-6)
            sum_w += w
            sum_r1 += pj["rain_1h_mm"] * w
            sum_r3 += pj["rain_3h_mm"] * w

        if sum_w > 0.0:
            neigh_r1 = sum_r1 / sum_w
            neigh_r3 = sum_r3 / sum_w
        else:
            neigh_r1 = 0.0
            neigh_r3 = 0.0

        pi["eff_rain_1h_mm"] = pi["rain_1h_mm"] + neighbor_weight * neigh_r1
        pi["eff_rain_3h_mm"] = pi["rain_3h_mm"] + neighbor_weight * neigh_r3


def _vulnerability_from_relief(relief_m):
    """
    Độ cao tương đối (m) -> vulnerability 0..1

    Relief nhỏ = gần đáy vùng trũng → dễ ngập hơn.

    Ví dụ:
      <= 1m    : 1.0
      1–3m     : 0.9
      3–7m     : 0.7
      7–15m    : 0.5
      15–30m   : 0.3
      > 30m    : 0.1
    """
    if relief_m is None or not math.isfinite(relief_m):
        return 0.3  # không có dữ liệu, coi như trung bình thấp

    if relief_m <= 1.0:
        return 1.0
    elif relief_m <= 3.0:
        return 0.9
    elif relief_m <= 7.0:
        return 0.7
    elif relief_m <= 15.0:
        return 0.5
    elif relief_m <= 30.0:
        return 0.3
    else:
        return 0.1


def _rain_factor(rain_1h, rain_3h):
    """
    Chuẩn hóa mưa 1h, 3h về [0..1].
    """
    rain_1h = float(rain_1h or 0.0)
    rain_3h = float(rain_3h or 0.0)

    # Mưa 3h
    if rain_3h <= 5:
        r3 = 0.0
    elif rain_3h <= 20:
        r3 = 0.3 * (rain_3h - 5) / (20 - 5)
    elif rain_3h <= 50:
        r3 = 0.3 + 0.4 * (rain_3h - 20) / (50 - 20)
    else:
        extra = min(rain_3h - 50, 50)
        r3 = 0.7 + 0.3 * (extra / 50.0)

    # Mưa 1h
    if rain_1h <= 5:
        r1 = 0.0
    elif rain_1h <= 20:
        r1 = 0.6 * (rain_1h - 5) / (20 - 5)
    elif rain_1h <= 50:
        r1 = 0.6 + 0.3 * (rain_1h - 20) / (50 - 20)
    else:
        extra = min(rain_1h - 50, 50)
        r1 = 0.9 + 0.1 * (extra / 50.0)

    rf = 0.6 * r3 + 0.4 * r1
    return max(0.0, min(1.0, rf))


def _combine_rain_relief_to_score(relief_m, eff_rain_1h, eff_rain_3h):
    """
    Kết hợp:
      - rain_factor ∈ [0..1] từ mưa hiệu dụng 1h/3h
      - vuln ∈ [0.1..1] từ relief
      - risk_cont = rain_factor^1.2 * (0.3 + 0.7 * vuln)
      - risk_score = round(risk_cont * 5) ∈ [0..5]
    """
    vuln = _vulnerability_from_relief(relief_m)
    rain_f = _rain_factor(eff_rain_1h, eff_rain_3h)

    base = 0.3 + 0.7 * vuln  # [0.3..1.0]
    risk_cont = (rain_f ** 1.2) * base
    risk_cont = max(0.0, min(1.0, risk_cont))

    score = int(round(risk_cont * 5))
    score = max(0, min(5, score))
    return score


    def _risk_score_to_level(score: int) -> str:
        if score >= 5:
            return "VERY_HIGH"
        elif score >= 4:
            return "HIGH"
        elif score >= 2:
            return "MODERATE"
        elif score >= 1:
            return "LOW"
        else:
            return "NONE"


def flood_risk_latest(request):
    """
    GET /api/obs/flood_risk_latest

    Bản nâng cấp:
      - Dùng mưa 1h/3h như cũ (weather_hourly_obs, source='openmeteo')
      - Thêm địa hình tương đối (relief_local) từ DEM quanh từng trạm
      - Không mưa tại điểm -> risk = 0 (NONE)
      - Có mưa tại điểm -> risk = f(mưa hiệu dụng, relief)
    """

    with connection.cursor() as cur:
        cur.execute(
            """
            WITH latest AS (
              SELECT
                MAX(valid_at) AS latest_valid_at
              FROM public.weather_hourly_obs
              WHERE source = 'openmeteo'
            ),
            rain AS (
              SELECT
                w.location_id,
                l.latest_valid_at,
                SUM(
                  CASE
                    WHEN w.valid_at >= l.latest_valid_at - interval '1 hour'
                         AND w.valid_at <= l.latest_valid_at
                    THEN COALESCE(w.precip_mm, 0)
                    ELSE 0
                  END
                ) AS rain_1h,
                SUM(
                  CASE
                    WHEN w.valid_at >= l.latest_valid_at - interval '3 hour'
                         AND w.valid_at <= l.latest_valid_at
                    THEN COALESCE(w.precip_mm, 0)
                    ELSE 0
                  END
                ) AS rain_3h
              FROM public.weather_hourly_obs w
              CROSS JOIN latest l
              WHERE w.source = 'openmeteo'
                AND l.latest_valid_at IS NOT NULL
                AND w.valid_at >= l.latest_valid_at - interval '3 hour'
                AND w.valid_at <= l.latest_valid_at
              GROUP BY w.location_id, l.latest_valid_at
            )
            SELECT
              loc.id,
              loc.lat,
              loc.lon,
              r.latest_valid_at,
              r.rain_1h,
              r.rain_3h
            FROM rain r
            JOIN public.locations loc
              ON loc.id = r.location_id
            ORDER BY r.latest_valid_at DESC, loc.lat, loc.lon;
            """
        )
        rows = cur.fetchall()

    if not rows:
        return JsonResponse({"count": 0, "data": []})

    points = []
    for row in rows:
        loc_id = str(row[0])
        lat = float(row[1])
        lon = float(row[2])
        latest_valid_at = row[3]  # datetime
        rain_1h = float(row[4] or 0.0)
        rain_3h = float(row[5] or 0.0)

        points.append(
            {
                "location_id": loc_id,
                "lat": lat,
                "lon": lon,
                "valid_at_dt": latest_valid_at,
                "rain_1h_mm": rain_1h,
                "rain_3h_mm": rain_3h,
            }
        )

    _add_neighborhood_rain(points, radius_km=20.0, neighbor_weight=0.5)

    data = []
    for p in points:
        loc_id = p["location_id"]
        lat = p["lat"]
        lon = p["lon"]
        latest_valid_at_dt = p["valid_at_dt"]
        rain_1h = p["rain_1h_mm"]
        rain_3h = p["rain_3h_mm"]
        eff_rain_1h = p.get("eff_rain_1h_mm", rain_1h)
        eff_rain_3h = p.get("eff_rain_3h_mm", rain_3h)

        # Không mưa tại điểm -> nguy cơ = 0 tuyệt đối
        if rain_1h <= 0.0 and rain_3h <= 0.0:
            score = 0
            level = "NONE"
            relief_m = sample_relief_local(lat, lon)  # chỉ để debug / hiển thị thêm
        else:
            relief_m = sample_relief_local(lat, lon)
            score = _combine_rain_relief_to_score(relief_m, eff_rain_1h, eff_rain_3h)
            level = _risk_score_to_level(score)

        data.append(
            {
                "location_id": loc_id,
                "lat": lat,
                "lon": lon,
                "valid_at": latest_valid_at_dt.isoformat()
                if latest_valid_at_dt is not None
                else None,
                "relief_m": relief_m,
                "rain_1h_mm": rain_1h,
                "rain_3h_mm": rain_3h,
                "eff_rain_1h_mm": eff_rain_1h,
                "eff_rain_3h_mm": eff_rain_3h,
                "risk_score": score,
                "risk_level": level,
            }
        )

    resp = JsonResponse({"count": len(data), "data": data})
    resp["Cache-Control"] = "public, max-age=60"
    return resp
