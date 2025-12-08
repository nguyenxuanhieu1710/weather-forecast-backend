# api/views_overview.py
import json
from django.db import connection
from django.http import JsonResponse


def _parse_raw(raw_val):
  """
  raw trong weather_hourly_obs đang lưu JSON kiểu:
    {"cloudcover": cc, "surface_pressure": sp}
  Ở đây chỉ cần cloudcover, có thì dùng, không có cũng được.
  """
  if not raw_val:
    return None
  try:
    if isinstance(raw_val, dict):
      obj = raw_val
    else:
      obj = json.loads(raw_val)
  except Exception:
    return None

  cc = obj.get("cloudcover")
  try:
    return float(cc) if cc is not None else None
  except Exception:
    return None


def obs_overview(request):
  """
  Tổng quan obs mới nhất trên toàn bộ mạng lưới:
  - Thời điểm quan trắc mới nhất (obs_time)
  - Điểm nóng nhất / mát nhất
  - Nhiệt độ trung bình
  - Số điểm đang mưa / mưa lớn
  - Số điểm nắng nóng (>= 35°C)
  - Số điểm gió mạnh
  """

  # Lấy snapshot mới nhất cho mỗi location_id
  # DISTINCT ON để lấy bản ghi mới nhất theo location_id
  with connection.cursor() as cur:
    cur.execute(
      """
      WITH latest AS (
        SELECT DISTINCT ON (w.location_id)
          w.location_id,
          w.valid_at,
          w.temp_c,
          w.precip_mm,
          w.wind_ms,
          w.raw
        FROM public.weather_hourly_obs w
        WHERE w.source = 'openmeteo'
        ORDER BY w.location_id, w.valid_at DESC
      )
      SELECT
        l.id,
        l.name,
        l.lat,
        l.lon,
        latest.valid_at,
        latest.temp_c,
        latest.precip_mm,
        latest.wind_ms,
        latest.raw
      FROM latest
      JOIN public.locations l
        ON l.id = latest.location_id;
      """
    )
    rows = cur.fetchall()

  if not rows:
    return JsonResponse(
      {
        "obs_time": None,
        "count_locations": 0,
        "temp": {},
        "rain": {},
        "wind": {},
      }
    )

  count = 0

  # Biến tổng hợp
  sum_temp = 0.0
  cnt_temp = 0

  max_temp = None
  max_temp_loc = None
  min_temp = None
  min_temp_loc = None

  raining_count = 0
  heavy_rain_count = 0  # ví dụ: mưa >= 5mm/h xem là mưa lớn nhẹ

  hot_count_35 = 0      # >= 35°C
  hot_count_37 = 0      # >= 37°C

  strong_wind_count = 0  # ví dụ: gió >= 10 m/s (~36 km/h)

  obs_time_latest = None

  # rows: (loc_id, loc_name, lat, lon, valid_at, temp_c, precip_mm, wind_ms, raw)
  for row in rows:
    (
      loc_id,
      loc_name,
      lat,
      lon,
      valid_at,
      temp_c,
      precip_mm,
      wind_ms,
      raw_val,
    ) = row

    count += 1

    # Thời gian quan trắc: lấy max valid_at làm obs_time
    if valid_at is not None:
      if obs_time_latest is None or valid_at > obs_time_latest:
        obs_time_latest = valid_at

    # Nhiệt độ
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

    # Mưa
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

    # Gió
    if wind_ms is not None:
      try:
        w_ms = float(wind_ms)
      except Exception:
        w_ms = None

      if w_ms is not None and w_ms >= 10.0:
        strong_wind_count += 1

  avg_temp = sum_temp / cnt_temp if cnt_temp > 0 else None

  data = {
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

  return JsonResponse(data)
