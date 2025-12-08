# api/views_obs.py
import math
from uuid import UUID
from datetime import timedelta, timezone as dt_timezone

from django.http import JsonResponse, HttpResponseBadRequest
from django.db import connection
from django.utils import timezone


def latest_snapshot(request):
    """
    Trả về bản ghi mới nhất của MỖI điểm (đọc từ MV latest_openmeteo_hourly)
    cho heatmap / frontend.
    """
    limit = int(request.GET.get("limit") or 0)  # optional

    sql = """
      SELECT
        l.id,
        l.lat,
        l.lon,
        w.valid_at,
        w.temp_c,
        w.wind_ms,
        w.precip_mm,
        w.wind_dir_deg,
        w.rel_humidity_pct,
        w.cloudcover_pct,
        w.surface_pressure_hpa
      FROM public.latest_openmeteo_hourly w
      JOIN public.locations l ON l.id = w.location_id
      ORDER BY w.valid_at DESC, l.lat, l.lon
    """
    params = []
    if limit > 0:
        sql += " LIMIT %s"
        params.append(limit)

    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    data = [
        {
            "location_id": str(r[0]),
            "lat": float(r[1]),
            "lon": float(r[2]),
            "valid_at": r[3].isoformat(),
            "temp_c": r[4],
            "wind_ms": r[5],
            "precip_mm": r[6],
            "wind_dir_deg": r[7],
            "rel_humidity_pct": r[8],
            "cloudcover_pct": r[9],
            "surface_pressure_hpa": r[10],
        }
        for r in rows
    ]

    resp = JsonResponse({"count": len(data), "data": data})
    resp["Cache-Control"] = "public, max-age=60"
    return resp


def merged_timeseries(request, location_id):
    """
    Chuỗi thời gian MERGED cho 1 điểm: 48h quá khứ + 96h tương lai (mặc định).

    - Quá khứ: ưu tiên OBS (weather_hourly_obs, source='openmeteo')
    - Tương lai: dùng FCST (weather_hourly_fcst) nếu có
    - Nếu giờ nào không có cả OBS lẫn FCST -> source = "none"
    """
    # validate UUID
    try:
        UUID(str(location_id))
    except Exception:
        return HttpResponseBadRequest("invalid location_id")

    # Lấy info location (id, name, lat, lon)
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, lat, lon
            FROM public.locations
            WHERE id = %s
            """,
            [str(location_id)],
        )
        loc_row = cur.fetchone()

    if not loc_row:
        return JsonResponse({"found": False})

    loc_info = {
        "id": str(loc_row[0]),
        "name": loc_row[1],
        "lat": float(loc_row[2]),
        "lon": float(loc_row[3]),
    }

    # Đọc tham số back/fwd/provider
    try:
        back_hours = int(request.GET.get("back") or 48)
    except Exception:
        back_hours = 48
    try:
        fwd_hours = int(request.GET.get("fwd") or 96)
    except Exception:
        fwd_hours = 96

    if back_hours < 0:
        back_hours = 0
    if back_hours > 168:
        back_hours = 168
    if fwd_hours < 0:
        fwd_hours = 0
    if fwd_hours > 168:
        fwd_hours = 168

    provider = (request.GET.get("provider") or "ml").strip() or "ml"

    # Giờ "base" = now UTC floored về đầu giờ
    now_utc = timezone.now().astimezone(dt_timezone.utc)
    base_utc = now_utc.replace(minute=0, second=0, microsecond=0)

    start_utc = base_utc - timedelta(hours=back_hours)
    end_utc = base_utc + timedelta(hours=fwd_hours)

    # ------------------ Lấy OBS trong khoảng [start_utc, end_utc] ------------------
    obs_map = {}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT
              valid_at,
              temp_c,
              wind_ms,
              precip_mm,
              rel_humidity_pct,
              wind_dir_deg,
              cloudcover_pct,
              surface_pressure_hpa
            FROM public.weather_hourly_obs
            WHERE source = 'openmeteo'
              AND location_id = %s
              AND valid_at >= %s
              AND valid_at <= %s
            ORDER BY valid_at ASC
            """,
            [str(location_id), start_utc, end_utc],
        )
        rows = cur.fetchall()

    for r in rows:
        ts = r[0]
        obs_map[ts] = {
            "temp_c": r[1],
            "wind_ms": r[2],
            "precip_mm": r[3],
            "rel_humidity_pct": r[4],
            "wind_dir_deg": r[5],
            "cloudcover_pct": r[6],
            "surface_pressure_hpa": r[7],
        }

    # ------------------ Lấy FCST trong khoảng [start_utc, end_utc] ------------------
    fcst_map = {}

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT
              valid_at,
              temp_c,
              wind_ms,
              precip_mm,
              rel_humidity_pct,
              wind_dir_deg,
              cloudcover_pct,
              surface_pressure_hpa
            FROM public.weather_hourly_fcst
            WHERE location_id = %s
              AND provider = %s
              AND valid_at >= %s
              AND valid_at <= %s
            ORDER BY valid_at ASC
            """,
            [str(location_id), provider, start_utc, end_utc],
        )
        rows = cur.fetchall()

    for r in rows:
        ts = r[0]
        fcst_map[ts] = {
            "temp_c": r[1],
            "wind_ms": r[2],
            "precip_mm": r[3],
            "rel_humidity_pct": r[4],
            "wind_dir_deg": r[5],
            "cloudcover_pct": r[6],
            "surface_pressure_hpa": r[7],
        }

    # ------------------ GHÉP MERGED THEO TRỤC THỜI GIAN ------------------
    steps = []
    cursor = start_utc

    while cursor <= end_utc:
        rec = None
        source = "none"

        if cursor in obs_map:
            rec = obs_map[cursor]
            source = "obs"
        elif cursor in fcst_map:
            rec = fcst_map[cursor]
            source = "fcst"

        steps.append(
            {
                "valid_at": cursor.isoformat(),
                "source": source,
                "temp_c": rec["temp_c"] if rec else None,
                "wind_ms": rec["wind_ms"] if rec else None,
                "precip_mm": rec["precip_mm"] if rec else None,
                "rel_humidity_pct": rec["rel_humidity_pct"] if rec else None,
                "wind_dir_deg": rec["wind_dir_deg"] if rec else None,
                "cloudcover_pct": rec["cloudcover_pct"] if rec else None,
                "surface_pressure_hpa": rec["surface_pressure_hpa"] if rec else None,
            }
        )

        cursor += timedelta(hours=1)

    return JsonResponse(
        {
            "found": True,
            "location": loc_info,
            "base_time": base_utc.isoformat(),
            "back_hours": back_hours,
            "forward_hours": fwd_hours,
            "count": len(steps),
            "steps": steps,
        }
    )


def nearest_point(request):
    """
    Trả về location gần nhất + OBS mới nhất tại điểm đó.
    """
    try:
        lat = float(request.GET["lat"])
        lon = float(request.GET["lon"])
    except Exception:
        return HttpResponseBadRequest("need lat & lon")

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT
              l.id,
              l.lat,
              l.lon,
              w.valid_at,
              w.temp_c,
              w.wind_ms,
              w.precip_mm,
              w.wind_dir_deg,
              w.rel_humidity_pct,
              w.cloudcover_pct,
              w.surface_pressure_hpa
            FROM public.locations l
            JOIN public.latest_openmeteo_hourly w
              ON w.location_id = l.id
            ORDER BY l.geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
            LIMIT 1
            """,
            [lon, lat],
        )
        row = cur.fetchone()

    if not row:
        return JsonResponse({"found": False})

    return JsonResponse(
        {
            "found": True,
            "location_id": str(row[0]),
            "lat": float(row[1]),
            "lon": float(row[2]),
            "valid_at": row[3].isoformat(),
            "temp_c": row[4],
            "wind_ms": row[5],
            "precip_mm": row[6],
            "wind_dir_deg": row[7],
            "rel_humidity_pct": row[8],
            "cloudcover_pct": row[9],
            "surface_pressure_hpa": row[10],
        }
    )


def rain_frames(request):
    """
    Radar mưa giả lập: trả nhiều frame mưa ở các mốc thời gian mới nhất.
    """
    try:
        n_frames = int(request.GET.get("frames") or 6)
    except Exception:
        return HttpResponseBadRequest("invalid frames")

    if n_frames < 1:
        n_frames = 1
    if n_frames > 24:
        n_frames = 24

    with connection.cursor() as cur:
        cur.execute(
            """
            WITH latest_times AS (
              SELECT DISTINCT valid_at
              FROM public.weather_hourly_obs
              WHERE source = 'openmeteo'
              ORDER BY valid_at DESC
              LIMIT %s
            )
            SELECT
              w.valid_at,
              l.lat,
              l.lon,
              w.precip_mm
            FROM public.weather_hourly_obs w
            JOIN public.locations l
              ON l.id = w.location_id
            JOIN latest_times t
              ON t.valid_at = w.valid_at
            WHERE w.source = 'openmeteo'
            ORDER BY w.valid_at ASC, l.lat, l.lon;
            """,
            [n_frames],
        )
        rows = cur.fetchall()

    frames_map = {}
    for valid_at, lat, lon, precip in rows:
        if precip is None:
            continue
        try:
            p = float(precip)
        except Exception:
            continue

        key = valid_at
        if key not in frames_map:
            frames_map[key] = []
        frames_map[key].append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "precip_mm": p,
            }
        )

    frames = []
    for ts in sorted(frames_map.keys()):
        cells = frames_map[ts]
        frames.append(
            {
                "valid_at": ts.isoformat(),
                "cells": cells,
            }
        )

    data = {
        "frame_count": len(frames),
        "frames": frames,
    }
    return JsonResponse(data)


def wind_trajectory(request):
    """
    Dự đoán quỹ đạo gió đơn giản từ 1 vị trí,
    dùng trường gió hiện tại (trung bình từ các điểm lân cận).
    """
    try:
        lat0 = float(request.GET["lat"])
        lon0 = float(request.GET["lon"])
    except Exception:
        return HttpResponseBadRequest("need lat & lon")

    try:
        hours = int(request.GET.get("hours") or 6)
    except Exception:
        return HttpResponseBadRequest("invalid hours")

    if hours < 1:
        hours = 1
    if hours > 24:
        hours = 24

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT
              l.lat,
              l.lon,
              w.wind_ms,
              w.wind_dir_deg
            FROM public.locations l
            JOIN public.latest_openmeteo_hourly w
              ON w.location_id = l.id
            ORDER BY l.geom <-> ST_SetSRID(ST_Point(%s,%s), 4326)
            LIMIT 10
            """,
            [lon0, lat0],
        )
        rows = cur.fetchall()

    if not rows:
        return JsonResponse({"found": False})

    sum_u = 0.0
    sum_v = 0.0
    cnt = 0

    for lat, lon, wind_ms, wind_dir_deg in rows:
        if wind_ms is None or wind_dir_deg is None:
            continue
        try:
            spd = float(wind_ms)
            ddeg = float(wind_dir_deg)
        except Exception:
            continue

        if spd <= 0:
            continue

        rad = math.radians(ddeg)
        u = spd * math.sin(rad)
        v = spd * math.cos(rad)

        sum_u += u
        sum_v += v
        cnt += 1

    if cnt == 0:
        return JsonResponse({"found": False})

    mean_u = sum_u / cnt
    mean_v = sum_v / cnt

    mean_speed = math.sqrt(mean_u**2 + mean_v**2)
    mean_dir_rad = math.atan2(mean_u, mean_v)
    mean_dir_deg = (math.degrees(mean_dir_rad) + 360.0) % 360.0

    step_hours = 1.0
    steps = hours

    points = []
    lat = lat0
    lon = lon0

    for i in range(steps + 1):
        points.append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "t_offset_h": i * step_hours,
            }
        )

        if i == steps:
            break

        dt_seconds = step_hours * 3600.0
        d_north_m = mean_v * dt_seconds
        d_east_m = mean_u * dt_seconds

        dlat_deg = d_north_m / 111000.0
        lat_rad = math.radians(lat)
        cos_lat = math.cos(lat_rad)
        if abs(cos_lat) < 1e-6:
            cos_lat = 1e-6
        dlon_deg = d_east_m / (111000.0 * cos_lat)

        lat += dlat_deg
        lon += dlon_deg

    data = {
        "found": True,
        "start": {"lat": float(lat0), "lon": float(lon0)},
        "hours": hours,
        "step_hours": step_hours,
        "mean_wind_ms": mean_speed,
        "mean_dir_deg": mean_dir_deg,
        "points": points,
    }
    return JsonResponse(data)
