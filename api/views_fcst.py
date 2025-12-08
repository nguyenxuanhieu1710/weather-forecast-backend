# api/views_fcst.py
#
# API forecast giờ từ bảng weather_hourly_fcst
# Lấy run mới nhất, trả timeseries cho 1 location_id

from uuid import UUID

from django.db import connection
from django.http import JsonResponse


# Số giờ forecast tối đa cho phép client yêu cầu
DEFAULT_HOURS = 48
MAX_HOURS = 168  # 7 ngày


def _get_latest_fcst_issued_at():
    """
    Lấy issued_at mới nhất của forecast.
    Ưu tiên đọc từ ml_run_status nếu có.
    Nếu không có bảng/row, fallback sang max(issued_at) trong weather_hourly_fcst.
    """
    with connection.cursor() as cur:
        # Thử đọc từ ml_run_status
        try:
            cur.execute(
                """
                select last_fcst_issued_at
                from public.ml_run_status
                where id = true;
                """
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return row[0]
        except Exception:
            # Nếu bảng chưa tồn tại hoặc lỗi gì đó, bỏ qua và dùng fallback
            pass

        # Fallback: dùng trực tiếp max(issued_at) trong bảng forecast
        cur.execute(
            """
            select max(issued_at)
            from public.weather_hourly_fcst
            where provider = 'ml';
            """
        )
        row = cur.fetchone()
        return row[0] if row else None


def hourly_fcst_latest_run(request, location_id):
    """
    GET /api/fcst/hourly/<uuid:location_id>?hours=48

    Trả về forecast giờ (tối đa N giờ) cho 1 location,
    dùng run ML mới nhất (issued_at mới nhất).
    """
    # Parse location_id
    try:
        loc_uuid = UUID(str(location_id))
    except ValueError:
        return JsonResponse({"error": "invalid location_id"}, status=400)

    # Parse query param hours
    hours_param = request.GET.get("hours")
    try:
        hours = int(hours_param) if hours_param is not None else DEFAULT_HOURS
    except ValueError:
        hours = DEFAULT_HOURS

    if hours <= 0:
        hours = DEFAULT_HOURS
    if hours > MAX_HOURS:
        hours = MAX_HOURS

    # Lấy issued_at mới nhất
    issued_at = _get_latest_fcst_issued_at()
    if issued_at is None:
        return JsonResponse(
            {"found": False, "reason": "no_forecast_run"},
            status=200,
        )

    with connection.cursor() as cur:
        # Lấy thông tin location (name, lat, lon) nếu cần trả ra
        cur.execute(
            """
            select id, name, lat, lon
            from public.locations
            where id = %s;
            """,
            [str(loc_uuid)],
        )
        loc_row = cur.fetchone()
        if not loc_row:
            return JsonResponse(
                {"found": False, "reason": "location_not_found"},
                status=404,
            )

        location = {
            "id": str(loc_row[0]),
            "name": loc_row[1],
            "lat": float(loc_row[2]) if loc_row[2] is not None else None,
            "lon": float(loc_row[3]) if loc_row[3] is not None else None,
        }

        # Lấy forecast cho run mới nhất, giới hạn theo horizon <= hours
        cur.execute(
            """
            select
                valid_at,
                horizon,
                temp_c,
                wind_ms,
                precip_mm,
                rel_humidity_pct,
                wind_dir_deg,
                cloudcover_pct,
                surface_pressure_hpa
            from public.weather_hourly_fcst
            where location_id = %s
              and issued_at = %s
              and horizon between 0 and %s
            order by valid_at;
            """,
            [str(loc_uuid), issued_at, hours],
        )

        rows = cur.fetchall()

    data = [
        {
            "valid_at": r[0].isoformat() if r[0] is not None else None,
            "horizon": r[1],
            "temp_c": r[2],
            "wind_ms": r[3],
            "precip_mm": r[4],
            "rel_humidity_pct": r[5],
            "wind_dir_deg": r[6],
            "cloudcover_pct": r[7],
            "surface_pressure_hpa": r[8],
        }
        for r in rows
    ]

    return JsonResponse(
        {
            "found": True,
            "location": location,
            "issued_at": issued_at.isoformat(),
            "hours": hours,
            "count": len(data),
            "data": data,
        }
    )
