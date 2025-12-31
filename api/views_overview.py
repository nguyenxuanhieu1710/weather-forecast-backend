# api/views_overview.py
from django.db import connection
from django.http import JsonResponse


def obs_overview(request):
    """
    Tổng quan latest toàn mạng.

    - Mỗi location chọn bản ghi mới nhất từ OBS(openmeteo) và FCST(ML).  (CHỈ SỬA GRU -> ML)
    - Nếu cùng valid_at: ưu tiên OBS.
    - JSON GIỮ NGUYÊN schema cũ.
    """
    with connection.cursor() as cur:
        cur.execute(
            """
            WITH merged AS (
              SELECT
                w.location_id,
                w.valid_at,
                w.temp_c,
                w.precip_mm,
                w.wind_ms,
                1 AS src_priority
              FROM public.weather_hourly_obs w
              WHERE w.source = 'openmeteo'

              UNION ALL

              SELECT
                f.location_id,
                f.valid_at,
                f.temp_c,
                f.precip_mm,
                f.wind_ms,
                2 AS src_priority
              FROM public.weather_hourly_fcst f
              WHERE f.provider = 'ML'
            ),
            latest AS (
              SELECT DISTINCT ON (m.location_id)
                m.location_id,
                m.valid_at,
                m.temp_c,
                m.precip_mm,
                m.wind_ms
              FROM merged m
              ORDER BY m.location_id, m.valid_at DESC, m.src_priority ASC
            )
            SELECT
              l.id,
              l.name,
              l.lat,
              l.lon,
              latest.valid_at,
              latest.temp_c,
              latest.precip_mm,
              latest.wind_ms
            FROM latest
            JOIN public.locations l ON l.id = latest.location_id;
            """
        )
        rows = cur.fetchall()

    if not rows:
        return JsonResponse(
            {"obs_time": None, "count_locations": 0, "temp": {}, "rain": {}, "wind": {}}
        )

    count = 0
    sum_temp = 0.0
    cnt_temp = 0

    max_temp = None
    max_temp_loc = None
    min_temp = None
    min_temp_loc = None

    raining_count = 0
    heavy_rain_count = 0  # >= 5mm/h

    hot_count_35 = 0
    hot_count_37 = 0

    strong_wind_count = 0  # >= 10 m/s

    obs_time_latest = None

    for (loc_id, loc_name, lat, lon, valid_at, temp_c, precip_mm, wind_ms) in rows:
        count += 1

        if valid_at is not None and (obs_time_latest is None or valid_at > obs_time_latest):
            obs_time_latest = valid_at

        if temp_c is not None:
            try:
                t = float(temp_c)
            except Exception:
                t = None
            if t is not None:
                sum_temp += t
                cnt_temp += 1

                if max_temp is None or t > max_temp:
                    max_temp = t
                    max_temp_loc = {
                        "id": str(loc_id),
                        "name": loc_name,
                        "lat": float(lat),
                        "lon": float(lon),
                        "temp_c": t,
                    }

                if min_temp is None or t < min_temp:
                    min_temp = t
                    min_temp_loc = {
                        "id": str(loc_id),
                        "name": loc_name,
                        "lat": float(lat),
                        "lon": float(lon),
                        "temp_c": t,
                    }

                if t >= 35.0:
                    hot_count_35 += 1
                if t >= 37.0:
                    hot_count_37 += 1

        if precip_mm is not None:
            try:
                p = float(precip_mm)
            except Exception:
                p = None
            if p is not None:
                if p > 0.0:
                    raining_count += 1
                if p >= 5.0:
                    heavy_rain_count += 1

        if wind_ms is not None:
            try:
                w = float(wind_ms)
            except Exception:
                w = None
            if w is not None and w >= 10.0:
                strong_wind_count += 1

    avg_temp = (sum_temp / cnt_temp) if cnt_temp > 0 else None

    return JsonResponse(
        {
            "obs_time": obs_time_latest.isoformat() if obs_time_latest else None,
            "count_locations": count,
            "temp": {
                "avg_c": avg_temp,
                "max_c": max_temp,
                "min_c": min_temp,
                "hottest": max_temp_loc,
                "coldest": min_temp_loc,
                "hot_count_ge_35": hot_count_35,
                "hot_count_ge_37": hot_count_37,
            },
            "rain": {
                "raining_count": raining_count,
                "heavy_rain_count": heavy_rain_count,
            },
            "wind": {
                "strong_wind_count": strong_wind_count,
            },
        }
    )
