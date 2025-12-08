# api/views.py
from datetime import datetime, timezone

from dateutil import parser as dateparser
from django.db import connection
from django.http import JsonResponse
from django.utils.timezone import now
from rest_framework.decorators import api_view


def _json(data, status=200):
    # safe=False để trả được cả list lẫn dict
    return JsonResponse(data, status=status, safe=False)


def _parse_ts(ts_str: str | None) -> datetime:
    """
    Parse ts về datetime timezone-aware (UTC), default = giờ hiện tại (ngang giờ).
    """
    if not ts_str:
        # truncate về đầu giờ hiện tại theo UTC
        t = now().astimezone(timezone.utc)
        return t.replace(minute=0, second=0, microsecond=0)
    try:
        t = dateparser.parse(ts_str)
        if t.tzinfo is None:
            # giả định UTC nếu client không kèm tz
            t = t.replace(tzinfo=timezone.utc)
        else:
            t = t.astimezone(timezone.utc)
        # chuẩn hoá về đầu giờ
        return t.replace(minute=0, second=0, microsecond=0)
    except Exception:
        # caller sẽ xử lý lỗi
        raise


@api_view(["GET"])
def health(request):
    with connection.cursor() as cur:
        cur.execute("SELECT 1;")
        one = cur.fetchone()[0]
    return _json({"ok": True, "db": one == 1})


@api_view(["GET"])
def locations(request):
    """
    Danh sách location (optional: lọc theo active, q, phân trang).
    """
    limit = int(request.GET.get("limit", 500))
    offset = int(request.GET.get("offset", 0))
    q = (request.GET.get("q") or "").strip()
    active = request.GET.get("active")

    sql = """
      SELECT id, name, lat, lon
      FROM public.locations
      WHERE (%(active)s IS NULL OR active = %(active)s)
        AND (%(q)s = '' OR name ILIKE %(q_like)s)
      ORDER BY name
      LIMIT %(limit)s OFFSET %(offset)s;
    """
    params = {
        "active": None if active is None else str(active).lower() in ("1", "true", "t", "yes", "y"),
        "q": q,
        "q_like": f"%{q}%",
        "limit": max(1, min(limit, 1000)),
        "offset": max(0, offset),
    }

    with connection.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    data = [
        {"id": r[0], "name": r[1], "lat": float(r[2]), "lon": float(r[3])}
        for r in rows
    ]
    return _json({"results": data, "limit": params["limit"], "offset": params["offset"]})


@api_view(["GET"])
def nowcast_hourly(request):
    """
    Ảnh chụp nowcast theo giờ (dùng v_nowcast_latest_hourly).
    Nếu không truyền ts -> dùng giờ hiện tại (UTC, truncate về đầu giờ).
    """
    ts = request.GET.get("ts")
    try:
        t = _parse_ts(ts)
    except Exception:
        return _json({"error": "Invalid ts"}, status=400)

    sql = """
      SELECT
        l.id,
        l.lat,
        l.lon,
        n.valid_at,
        n.temp_c,
        n.wind_ms,
        n.precip_mm,
        n.wind_dir_deg,
        n.rel_humidity_pct,
        n.cloudcover_pct,
        n.surface_pressure_hpa,
        n.kind,
        n.provider
      FROM public.v_nowcast_latest_hourly n
      JOIN public.locations l ON l.id = n.location_id
      WHERE n.valid_at = date_trunc('hour', %(ts)s::timestamptz)
    """
    with connection.cursor() as cur:
        cur.execute(sql, {"ts": t})
        rows = cur.fetchall()

    data = []
    for r in rows:
        data.append(
            {
                "location_id": r[0],
                "lat": float(r[1]),
                "lon": float(r[2]),
                "valid_at": r[3].astimezone(timezone.utc).isoformat(),
                "temp_c": None if r[4] is None else float(r[4]),
                "wind_ms": None if r[5] is None else float(r[5]),
                "precip_mm": None if r[6] is None else float(r[6]),
                "wind_dir_deg": None if r[7] is None else float(r[7]),
                "rel_humidity_pct": None if r[8] is None else float(r[8]),
                "cloudcover_pct": None if r[9] is None else float(r[9]),
                "surface_pressure_hpa": None if r[10] is None else float(r[10]),
                "kind": r[11],
                "provider": r[12],
            }
        )

    return _json(data)


@api_view(["GET"])
def geojson_hourly(request):
    """
    Trả GeoJSON heatmap cho 1 biến tại 1 thời điểm (theo giờ).

    var hỗ trợ:
      - temp        -> temp_c
      - wind        -> wind_ms
      - rain        -> precip_mm
      - humidity    -> rel_humidity_pct
      - cloud       -> cloudcover_pct
      - pressure    -> surface_pressure_hpa
    """
    var = (request.GET.get("var") or "temp").lower()
    var_map = {
        "temp": "temp_c",
        "wind": "wind_ms",
        "rain": "precip_mm",
        "humidity": "rel_humidity_pct",
        "cloud": "cloudcover_pct",
        "pressure": "surface_pressure_hpa",
    }
    col = var_map.get(var)
    if not col:
        return _json({"error": "var must be one of temp|wind|rain|humidity|cloud|pressure"}, status=400)

    ts = request.GET.get("ts")
    try:
        t = _parse_ts(ts)
    except Exception:
        return _json({"error": "Invalid ts"}, status=400)

    # col đã được whitelist nên an toàn cho f-string
    sql = f"""
      WITH data AS (
        SELECT l.geom, n.{col} AS val
        FROM public.v_nowcast_latest_hourly n
        JOIN public.locations l ON l.id = n.location_id
        WHERE n.valid_at = date_trunc('hour', %(ts)s::timestamptz)
      )
      SELECT jsonb_build_object(
        'type','FeatureCollection',
        'features', COALESCE(jsonb_agg(
          jsonb_build_object(
            'type','Feature',
            'geometry', ST_AsGeoJSON(geom)::jsonb,
            'properties', jsonb_build_object('value', val)
          )
        ), '[]'::jsonb)
      ) AS fc
      FROM data;
    """
    with connection.cursor() as cur:
        cur.execute(sql, {"ts": t})
        row = cur.fetchone()

    return _json(row[0] or {"type": "FeatureCollection", "features": []})
