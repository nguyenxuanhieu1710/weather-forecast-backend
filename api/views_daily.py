# api/views_daily.py
from uuid import UUID
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.http import JsonResponse, HttpResponseBadRequest
from django.db import connection
from django.utils import timezone

# dùng chung cấu hình provider với views_obs
from .views_obs import ALLOWED_PROVIDERS, DEFAULT_PROVIDER


def daily_summary(request, location_id):
    """
    API daily cho 1 điểm (location_id).

    Quy tắc:
      - Dải ngày: [today-2, today+4] theo local time (Asia/Bangkok).
      - Mỗi ngày tính từ 00:00 đến 23:00 LOCAL.
      - Mức giờ:
          + Nếu có OBS (weather_hourly_obs, source='openmeteo') -> dùng OBS.
          + Nếu không có OBS nhưng có FCST (weather_hourly_fcst, provider=?provider) -> dùng FCST.
          + Nếu không có cả hai -> bỏ qua giờ đó (không tính).

      - Mức ngày:
          + Quá khứ (date < today): dùng toàn bộ giờ 0–23, với fallback OBS/FCST như trên.
          + Hôm nay (date == today): mix OBS + FCST tùy dữ liệu có trong DB.
          + Tương lai (date > today): chủ yếu từ FCST, thiếu giờ thì bỏ qua.
    """
    # ----- Validate location_id -----
    try:
        UUID(str(location_id))
    except Exception:
        return HttpResponseBadRequest("invalid location_id")

    # ----- Lấy info location -----
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

    # ----- Thời gian local / dải ngày -----
    default_tz = timezone.get_default_timezone()  # Asia/Bangkok trong settings
    now_local = timezone.now().astimezone(default_tz)
    today_local = now_local.date()

    days_back = 2
    days_forward = 4

    start_date = today_local - timedelta(days=days_back)
    end_date = today_local + timedelta(days=days_forward)

    # 00:00 ngày start_date -> 23:00 ngày end_date, theo LOCAL
    start_local_dt = datetime.combine(start_date, time(0, 0, 0))
    end_local_dt = datetime.combine(end_date, time(23, 0, 0))

    start_local_aware = timezone.make_aware(start_local_dt, default_tz)
    end_local_aware = timezone.make_aware(end_local_dt, default_tz)

    # Convert sang UTC để query DB (timestamptz)
    start_utc = start_local_aware.astimezone(dt_timezone.utc)
    end_utc = end_local_aware.astimezone(dt_timezone.utc)

    # Provider FCST: lấy từ query param, kiểm tra hợp lệ
    raw_provider = (request.GET.get("provider") or "").strip()
    if raw_provider in ALLOWED_PROVIDERS:
        provider = raw_provider
    else:
        provider = DEFAULT_PROVIDER

    # ===== Đọc OBS trong khoảng [start_utc, end_utc] =====
    obs_map = {}   # key: (local_date, hour) -> record dict

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
            WHERE location_id = %s
              AND source = 'openmeteo'
              AND valid_at >= %s
              AND valid_at <= %s
            ORDER BY valid_at ASC
            """,
            [str(location_id), start_utc, end_utc],
        )
        rows = cur.fetchall()

    for r in rows:
        valid_at_utc = r[0]
        local_dt = valid_at_utc.astimezone(default_tz)
        d = local_dt.date()
        h = local_dt.hour
        key = (d, h)
        obs_map[key] = {
            "temp_c": r[1],
            "wind_ms": r[2],
            "precip_mm": r[3],
            "rel_humidity_pct": r[4],
            "wind_dir_deg": r[5],
            "cloudcover_pct": r[6],
            "surface_pressure_hpa": r[7],
        }

    # ===== Đọc FCST trong khoảng [start_utc, end_utc] =====
    fcst_map = {}  # key: (local_date, hour) -> record dict

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
        valid_at_utc = r[0]
        local_dt = valid_at_utc.astimezone(default_tz)
        d = local_dt.date()
        h = local_dt.hour
        key = (d, h)
        # fcst dùng nếu không có obs ở cùng giờ
        fcst_map[key] = {
            "temp_c": r[1],
            "wind_ms": r[2],
            "precip_mm": r[3],
            "rel_humidity_pct": r[4],
            "wind_dir_deg": r[5],
            "cloudcover_pct": r[6],
            "surface_pressure_hpa": r[7],
        }

    # ===== Tính daily cho từng ngày =====
    days_result = []

    d = start_date
    while d <= end_date:
        # Xác định loại ngày
        if d < today_local:
            kind = "past"
        elif d == today_local:
            kind = "today"
        else:
            kind = "future"

        # Tích lũy thống kê
        temp_values = []
        precip_sum = 0.0
        wind_values = []
        cloud_values = []

        hour_count = 0
        obs_hours = 0
        fcst_hours = 0

        for h in range(24):
            key = (d, h)

            rec = None
            src = None

            # Ưu tiên OBS, thiếu thì FCST
            if key in obs_map:
                rec = obs_map[key]
                src = "obs"
            elif key in fcst_map:
                rec = fcst_map[key]
                src = "fcst"
            else:
                # không có dữ liệu giờ này -> bỏ
                continue

            hour_count += 1
            if src == "obs":
                obs_hours += 1
            elif src == "fcst":
                fcst_hours += 1

            # temp
            t = rec.get("temp_c")
            if t is not None:
                try:
                    temp_values.append(float(t))
                except Exception:
                    pass

            # precip
            p = rec.get("precip_mm")
            if p is not None:
                try:
                    precip_sum += float(p)
                except Exception:
                    pass

            # wind
            w = rec.get("wind_ms")
            if w is not None:
                try:
                    wind_values.append(float(w))
                except Exception:
                    pass

            # cloudcover
            cc = rec.get("cloudcover_pct")
            if cc is not None:
                try:
                    cloud_values.append(float(cc))
                except Exception:
                    pass

        # Tính toán min / max / mean, nếu có dữ liệu
        if temp_values:
            temp_min = min(temp_values)
            temp_max = max(temp_values)
            temp_mean = sum(temp_values) / len(temp_values)
        else:
            temp_min = None
            temp_max = None
            temp_mean = None

        if wind_values:
            wind_mean = sum(wind_values) / len(wind_values)
        else:
            wind_mean = None

        if cloud_values:
            cloud_mean = sum(cloud_values) / len(cloud_values)
        else:
            cloud_mean = None

        missing_hours = 24 - hour_count
        if missing_hours < 0:
            missing_hours = 0

        days_result.append(
            {
                "date": d.isoformat(),
                "kind": kind,
                "hour_count": hour_count,
                "obs_hours": obs_hours,
                "fcst_hours": fcst_hours,
                "missing_hours": missing_hours,
                "temp_min_c": temp_min,
                "temp_max_c": temp_max,
                "temp_mean_c": temp_mean,
                "precip_sum_mm": precip_sum if hour_count > 0 else None,
                "wind_mean_ms": wind_mean,
                "cloudcover_mean_pct": cloud_mean,
            }
        )

        d += timedelta(days=1)

    return JsonResponse(
        {
            "found": True,
            "location": loc_info,
            "timezone": str(default_tz),
            "today": today_local.isoformat(),
            "provider": provider,
            "days_back": days_back,
            "days_forward": days_forward,
            "days": days_result,
        }
    )
# api/views_daily.py
from uuid import UUID
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.http import JsonResponse, HttpResponseBadRequest
from django.db import connection
from django.utils import timezone

# dùng chung cấu hình provider với views_obs
from .views_obs import ALLOWED_PROVIDERS, DEFAULT_PROVIDER


def daily_summary(request, location_id):
    """
    API daily cho 1 điểm (location_id).

    Quy tắc:
      - Dải ngày: [today-2, today+4] theo local time (Asia/Bangkok).
      - Mỗi ngày tính từ 00:00 đến 23:00 LOCAL.
      - Mức giờ:
          + Nếu có OBS (weather_hourly_obs, source='openmeteo') -> dùng OBS.
          + Nếu không có OBS nhưng có FCST (weather_hourly_fcst, provider=?provider) -> dùng FCST.
          + Nếu không có cả hai -> bỏ qua giờ đó (không tính).

      - Mức ngày:
          + Quá khứ (date < today): dùng toàn bộ giờ 0–23, với fallback OBS/FCST như trên.
          + Hôm nay (date == today): mix OBS + FCST tùy dữ liệu có trong DB.
          + Tương lai (date > today): chủ yếu từ FCST, thiếu giờ thì bỏ qua.
    """
    # ----- Validate location_id -----
    try:
        UUID(str(location_id))
    except Exception:
        return HttpResponseBadRequest("invalid location_id")

    # ----- Lấy info location -----
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

    # ----- Thời gian local / dải ngày -----
    default_tz = timezone.get_default_timezone()  # Asia/Bangkok trong settings
    now_local = timezone.now().astimezone(default_tz)
    today_local = now_local.date()

    days_back = 2
    days_forward = 4

    start_date = today_local - timedelta(days=days_back)
    end_date = today_local + timedelta(days=days_forward)

    # 00:00 ngày start_date -> 23:00 ngày end_date, theo LOCAL
    start_local_dt = datetime.combine(start_date, time(0, 0, 0))
    end_local_dt = datetime.combine(end_date, time(23, 0, 0))

    start_local_aware = timezone.make_aware(start_local_dt, default_tz)
    end_local_aware = timezone.make_aware(end_local_dt, default_tz)

    # Convert sang UTC để query DB (timestamptz)
    start_utc = start_local_aware.astimezone(dt_timezone.utc)
    end_utc = end_local_aware.astimezone(dt_timezone.utc)

    # Provider FCST: lấy từ query param, kiểm tra hợp lệ
    raw_provider = (request.GET.get("provider") or "").strip()
    if raw_provider in ALLOWED_PROVIDERS:
        provider = raw_provider
    else:
        provider = DEFAULT_PROVIDER

    # ===== Đọc OBS trong khoảng [start_utc, end_utc] =====
    obs_map = {}  # key: (local_date, hour) -> record dict

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
            WHERE location_id = %s
              AND source = 'openmeteo'
              AND valid_at >= %s
              AND valid_at <= %s
            ORDER BY valid_at ASC
            """,
            [str(location_id), start_utc, end_utc],
        )
        rows = cur.fetchall()

    for r in rows:
        valid_at_utc = r[0]
        local_dt = valid_at_utc.astimezone(default_tz)
        d = local_dt.date()
        h = local_dt.hour
        key = (d, h)
        obs_map[key] = {
            "temp_c": r[1],
            "wind_ms": r[2],
            "precip_mm": r[3],
            "rel_humidity_pct": r[4],
            "wind_dir_deg": r[5],
            "cloudcover_pct": r[6],
            "surface_pressure_hpa": r[7],
        }

    # ===== Đọc FCST trong khoảng [start_utc, end_utc] =====
    fcst_map = {}  # key: (local_date, hour) -> record dict

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
        valid_at_utc = r[0]
        local_dt = valid_at_utc.astimezone(default_tz)
        d = local_dt.date()
        h = local_dt.hour
        key = (d, h)
        fcst_map[key] = {
            "temp_c": r[1],
            "wind_ms": r[2],
            "precip_mm": r[3],
            "rel_humidity_pct": r[4],
            "wind_dir_deg": r[5],
            "cloudcover_pct": r[6],
            "surface_pressure_hpa": r[7],
        }

    # ===== Tính daily cho từng ngày =====
    days_result = []

    d = start_date
    while d <= end_date:
        if d < today_local:
            kind = "past"
        elif d == today_local:
            kind = "today"
        else:
            kind = "future"

        temp_values = []
        precip_sum = 0.0
        wind_values = []
        cloud_values = []

        hour_count = 0
        obs_hours = 0
        fcst_hours = 0

        for h in range(24):
            key = (d, h)

            rec = None
            src = None

            if key in obs_map:
                rec = obs_map[key]
                src = "obs"
            elif key in fcst_map:
                rec = fcst_map[key]
                src = "fcst"
            else:
                continue

            hour_count += 1
            if src == "obs":
                obs_hours += 1
            elif src == "fcst":
                fcst_hours += 1

            t = rec.get("temp_c")
            if t is not None:
                try:
                    temp_values.append(float(t))
                except Exception:
                    pass

            p = rec.get("precip_mm")
            if p is not None:
                try:
                    precip_sum += float(p)
                except Exception:
                    pass

            w = rec.get("wind_ms")
            if w is not None:
                try:
                    wind_values.append(float(w))
                except Exception:
                    pass

            cc = rec.get("cloudcover_pct")
            if cc is not None:
                try:
                    cloud_values.append(float(cc))
                except Exception:
                    pass

        if temp_values:
            temp_min = min(temp_values)
            temp_max = max(temp_values)
            temp_mean = sum(temp_values) / len(temp_values)
        else:
            temp_min = None
            temp_max = None
            temp_mean = None

        if wind_values:
            wind_mean = sum(wind_values) / len(wind_values)
        else:
            wind_mean = None

        if cloud_values:
            cloud_mean = sum(cloud_values) / len(cloud_values)
        else:
            cloud_mean = None

        missing_hours = 24 - hour_count
        if missing_hours < 0:
            missing_hours = 0

        days_result.append(
            {
                "date": d.isoformat(),
                "kind": kind,
                "hour_count": hour_count,
                "obs_hours": obs_hours,
                "fcst_hours": fcst_hours,
                "missing_hours": missing_hours,
                "temp_min_c": temp_min,
                "temp_max_c": temp_max,
                "temp_mean_c": temp_mean,
                "precip_sum_mm": precip_sum if hour_count > 0 else None,
                "wind_mean_ms": wind_mean,
                "cloudcover_mean_pct": cloud_mean,
            }
        )

        d += timedelta(days=1)

    return JsonResponse(
        {
            "found": True,
            "location": loc_info,
            "timezone": str(default_tz),
            "today": today_local.isoformat(),
            "provider": provider,
            "days_back": days_back,
            "days_forward": days_forward,
            "days": days_result,
        }
    )
