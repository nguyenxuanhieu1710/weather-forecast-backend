# api/views_alerts.py
from uuid import UUID
from datetime import timezone as dt_timezone
from typing import List, Tuple, Optional, Dict, Any

from django.db import connection
from django.http import JsonResponse, HttpResponseBadRequest
from django.utils import timezone


# =============================================================================
# 1) SUMMARY (mô tả ngắn gọn cho UI)
# =============================================================================

def _build_today_comment(temp_c: Optional[float], precip_mm: Optional[float], cloudcover: Optional[float]) -> str:
    """
    Mô tả tổng quan 'hôm nay' dựa trên snapshot hiện tại:
    - Nếu có mưa (>=0.1mm) => mô tả mưa
    - Nếu không mưa => mô tả theo mây
    - Thêm cảm nhận nhiệt độ
    """
    parts: List[str] = []

    if precip_mm is not None and precip_mm >= 1.0:
        parts.append("Có mưa, trời ẩm ướt")
    elif precip_mm is not None and precip_mm >= 0.1:
        parts.append("Mưa rào nhẹ, có thể trơn trượt")
    else:
        if cloudcover is None:
            parts.append("Trời khô ráo")
        elif cloudcover < 20:
            parts.append("Trời nắng, ít mây")
        elif cloudcover < 60:
            parts.append("Trời nắng gián đoạn, mây vừa")
        elif cloudcover < 85:
            parts.append("Nhiều mây, ít nắng")
        else:
            parts.append("Trời u ám")

    if temp_c is not None:
        if temp_c <= 18:
            parts.append("khá lạnh")
        elif temp_c <= 24:
            parts.append("mát mẻ, dễ chịu")
        elif temp_c <= 30:
            parts.append("ấm, hơi nóng vào trưa")
        else:
            parts.append("nóng, nên hạn chế hoạt động ngoài trời lúc trưa")

    return ". ".join(parts)


def _build_current_comment(temp_c: Optional[float], wind_ms: Optional[float], precip_mm: Optional[float]) -> str:
    """
    Mô tả 'hiện tại' (dùng cho panel).
    Lưu ý: Frontend hiện đang hiển thị nhiệt độ số lớn + câu mô tả. Do yêu cầu giữ nguyên JSON
    và tránh thay đổi hành vi bất ngờ, vẫn giữ câu "Nhiệt độ hiện tại..." như code cũ.
    """
    pieces: List[str] = []

    if temp_c is not None:
        pieces.append(f"Nhiệt độ hiện tại khoảng {temp_c:.1f}°C")

    if wind_ms is not None:
        try:
            wind_kmh = float(wind_ms) * 3.6
            if wind_kmh < 5:
                pieces.append("gió yếu")
            elif wind_kmh < 20:
                pieces.append("gió nhẹ đến vừa")
            else:
                pieces.append("gió khá mạnh")
        except Exception:
            pass

    if precip_mm is not None and precip_mm >= 0.1:
        pieces.append("có mưa")
    elif precip_mm is not None:
        pieces.append("không mưa")

    return ", ".join(pieces)


# =============================================================================
# 2) ALERTS ENGINE (rule-based hazards)
# =============================================================================

_LEVEL_ORDER = ["none", "info", "watch", "warning", "danger"]
_LEVEL_RANK = {lv: i for i, lv in enumerate(_LEVEL_ORDER)}


def _make_hazard(h_type: str, level: str, score: int, headline: str, description: str, advices: Optional[List[str]] = None) -> Dict[str, Any]:
    if advices is None:
        advices = []
    if level not in _LEVEL_RANK:
        level = "info"
    return {
        "type": h_type,
        "level": level,
        "score": int(score),
        "headline": headline,
        "description": description,
        "advices": advices,
    }


def _compute_heat_index(temp_c: Optional[float], rel_humidity: Optional[float]) -> Optional[float]:
    if temp_c is None:
        return None
    T = float(temp_c)
    if rel_humidity is None:
        return T
    RH = max(0.0, min(100.0, float(rel_humidity)))
    if T < 27 or RH < 40:
        return T
    return (
        -8.784695
        + 1.61139411 * T
        + 2.338549 * RH
        - 0.14611605 * T * RH
        - 0.012308094 * (T ** 2)
        - 0.016424828 * (RH ** 2)
        + 0.002211732 * (T ** 2) * RH
        + 0.00072546 * T * (RH ** 2)
        - 0.000003582 * (T ** 2) * (RH ** 2)
    )


def _compute_windchill(temp_c: Optional[float], wind_ms: Optional[float]) -> Optional[float]:
    if temp_c is None or wind_ms is None:
        return None
    T = float(temp_c)
    V_kmh = float(wind_ms) * 3.6
    if T > 10 or V_kmh < 5:
        return None
    return 13.12 + 0.6215 * T - 11.37 * (V_kmh ** 0.16) + 0.3965 * T * (V_kmh ** 0.16)


def _build_heat_cold_hazard(
    temp_c: Optional[float],
    wind_ms: Optional[float],
    cloudcover: Optional[float],
    rel_humidity: Optional[float],
) -> Optional[Dict[str, Any]]:
    """
    Hazard nhiệt/lạnh:
    - Lạnh: dựa trên windchill (nếu có) hoặc temp
    - Nóng: dựa trên heat index (nếu có), có xét mây nhiều
    """
    if temp_c is None:
        return None
    T = float(temp_c)

    windchill = _compute_windchill(T, wind_ms)
    effective_cold = windchill if windchill is not None else T

    # LẠNH
    if effective_cold <= 15:
        if effective_cold <= 5:
            level, score = "warning", 3
            headline = "Trời rét, có gió"
            desc = (
                f"Nhiệt độ cảm nhận xuống khoảng {effective_cold:.1f}°C "
                "(tính theo nhiệt độ và gió), dễ gây rét buốt, "
                "đặc biệt vào đêm và sáng sớm."
            )
        elif effective_cold <= 10:
            level, score = "watch", 2
            headline = "Trời lạnh"
            desc = (
                f"Nhiệt độ cảm nhận khoảng {effective_cold:.1f}°C, "
                "trời lạnh, cần chú ý giữ ấm cho trẻ nhỏ và người già."
            )
        else:
            level, score = "info", 1
            headline = "Thời tiết se lạnh"
            desc = f"Nhiệt độ khoảng {T:.1f}°C, khá mát hoặc hơi lạnh về đêm và sáng sớm."

        adv = [
            "Chuẩn bị áo ấm khi ra ngoài, đặc biệt vào tối và sáng sớm.",
            "Giữ ấm cho trẻ nhỏ, người cao tuổi.",
        ]
        return _make_hazard("cold", level, score, headline, desc, adv)

    # NÓNG (chỉ xét khi nhiệt độ đủ cao)
    if T < 32:
        return None

    HI = _compute_heat_index(T, rel_humidity)
    effective_heat = HI if HI is not None else T
    if effective_heat < 32:
        return None

    cloudy = cloudcover is not None and float(cloudcover) >= 60

    if effective_heat < 41:
        level, score = "info", 1
        headline = "Thời tiết oi nóng"
        desc = (
            f"Nhiệt độ cảm nhận khoảng {effective_heat:.1f}°C (tính theo nhiệt độ và độ ẩm), "
            "trời oi, dễ mệt nếu hoạt động ngoài trời lâu."
        )
    elif effective_heat < 54:
        level, score = "watch", 2
        headline = "Nắng nóng, cần thận trọng"
        desc = (
            f"Nhiệt độ cảm nhận khoảng {effective_heat:.1f}°C. "
            "Nếu làm việc ngoài trời lâu, nguy cơ mất nước và kiệt sức tăng."
        )
    else:
        level, score = "warning", 3
        headline = "Nắng nóng gay gắt, nguy cơ cao"
        desc = (
            f"Nhiệt độ cảm nhận trên {effective_heat:.1f}°C, "
            "nguy cơ say nắng, sốc nhiệt nếu ở ngoài trời trong thời gian dài."
        )

    if cloudy:
        desc += " Lượng mây nhiều có thể giảm bớt nắng trực tiếp, nhưng không giảm nhiều mức oi nóng."

    adv = [
        "Hạn chế ở ngoài trời nắng lâu trong khung giờ trưa - đầu giờ chiều.",
        "Uống đủ nước, mặc quần áo thoáng mát.",
        "Ưu tiên nghỉ ngơi trong bóng râm hoặc nơi có mái che.",
    ]
    return _make_hazard("heat", level, score, headline, desc, adv)


def _build_wind_hazard(wind_ms: Optional[float]) -> Optional[Dict[str, Any]]:
    if wind_ms is None:
        return None
    wind_kmh = float(wind_ms) * 3.6
    if wind_kmh < 20:
        return None

    if wind_kmh < 30:
        level, score = "info", 1
        headline = "Gió vừa"
        desc = (
            f"Tốc độ gió khoảng {wind_kmh:.0f} km/h (xấp xỉ cấp 4 Beaufort), "
            "có thể gây khó chịu khi di chuyển bằng xe máy hoặc đi bộ."
        )
    elif wind_kmh < 50:
        level, score = "watch", 2
        headline = "Gió mạnh"
        desc = (
            f"Gió mạnh khoảng {wind_kmh:.0f} km/h (xấp xỉ cấp 5–6 Beaufort), "
            "cần chú ý khi đi lại ngoài trời, đặc biệt ở nơi trống trải."
        )
    else:
        level, score = "warning", 3
        headline = "Gió rất mạnh"
        desc = (
            f"Gió rất mạnh khoảng {wind_kmh:.0f} km/h (từ cấp 7 Beaufort trở lên), "
            "có thể gây đổ cây, biển quảng cáo, nguy hiểm khi tham gia giao thông."
        )

    adv = [
        "Hạn chế đứng gần cây lớn, biển quảng cáo, vật dễ đổ.",
        "Giữ chắc tay lái nếu di chuyển bằng xe máy.",
    ]
    return _make_hazard("strong_wind", level, score, headline, desc, adv)


def _build_rain_hazard(
    rain_1h: float,
    rain_3h: float,
    rain_6h: float,
    precip_now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Hazard mưa dựa trên mưa tích lũy (1–3–6h).
    Quy ước trong đồ án:
    - Nếu đang mưa (precip_now >= 0.1) thì tối thiểu hazard info ("Mưa nhẹ"),
      kể cả khi tổng tích lũy chưa lớn.
    - Nếu tích lũy lớn hơn => nâng cấp watch/warning/danger theo ngưỡng.
    """
    r1 = float(rain_1h or 0.0)
    r3 = float(rain_3h or 0.0)
    r6 = float(rain_6h or 0.0)

    # Eff rain: ưu tiên 1h, cộng phần tăng thêm 3h/6h với trọng số
    extra_3h = max(0.0, r3 - r1)
    extra_6h = max(0.0, r6 - r3)
    eff = r1 + 0.6 * extra_3h + 0.4 * extra_6h

    # (A) Mưa nhẹ theo giờ hiện tại
    try:
        if precip_now is not None and float(precip_now) >= 0.1 and eff < 0.5:
            level, score = "info", 1
            headline = "Mưa nhẹ"
            desc = "Có mưa nhẹ trong giờ gần nhất, đường có thể trơn trượt."
            adv = ["Di chuyển cẩn thận, chuẩn bị áo mưa nếu cần."]
            return _make_hazard("rain", level, score, headline, desc, adv)
    except Exception:
        pass

    # (B) Không mưa đáng kể
    if eff < 0.5:
        return None

    # (C) Có mưa tích lũy -> phân cấp
    if eff < 5:
        level, score = "info", 1
        headline = "Mưa nhỏ, rải rác"
        desc = (
            f"Mưa tích lũy khoảng {eff:.1f}mm trong 1–6 giờ gần nhất. "
            "Ảnh hưởng chủ yếu là trơn trượt, bất tiện khi di chuyển."
        )
        adv = ["Chuẩn bị áo mưa nếu phải di chuyển ngoài trời."]
    elif eff < 15:
        level, score = "watch", 2
        headline = "Mưa vừa, có nguy cơ ngập nhẹ"
        desc = (
            f"Mưa tích lũy khoảng {eff:.1f}mm trong vài giờ gần đây, "
            "có thể gây ngập nhẹ tại các khu vực trũng, thoát nước kém."
        )
        adv = [
            "Hạn chế di chuyển nhanh trên đường trơn.",
            "Theo dõi các điểm trũng, khu dân cư thấp.",
        ]
    elif eff < 30:
        level, score = "warning", 3
        headline = "Mưa to, nguy cơ ngập cục bộ"
        desc = (
            f"Mưa tích lũy khoảng {eff:.1f}mm, "
            "nguy cơ ngập cục bộ tại khu dân cư, đô thị và các điểm trũng."
        )
        adv = [
            "Hạn chế đi qua vùng ngập nước hoặc khu vực thoát nước kém.",
            "Chủ động kê cao đồ đạc ở tầng thấp.",
        ]
    else:
        level, score = "danger", 4
        headline = "Mưa rất to, nguy cơ lũ cục bộ"
        desc = (
            f"Mưa tích lũy trên {eff:.1f}mm trong 6 giờ, "
            "nguy cơ ngập sâu, lũ quét hoặc sạt lở đất (đặc biệt vùng đồi núi)."
        )
        adv = [
            "Tránh di chuyển qua khu vực ngập sâu, sông suối, khe suối.",
            "Theo dõi chặt chẽ cảnh báo của cơ quan khí tượng thủy văn.",
        ]

    return _make_hazard("rain", level, score, headline, desc, adv)


def _calc_overall_level_and_comment(hazards: List[Dict[str, Any]]) -> Tuple[str, str]:
    """
    Tổng hợp mức cảnh báo:
    - Nếu không có hazard => none + câu an toàn
    - Nếu có hazard: chọn hazard có level cao nhất làm "main"
    - Nếu có >=2 hazard từ watch trở lên => tăng overall_rank lên 1 bậc (tối đa danger)
    """
    if not hazards:
        return ("none", "Thời tiết nhìn chung ổn định, không có nguy cơ đáng kể trong giờ gần nhất.")

    def _rank(h: Dict[str, Any]) -> int:
        return _LEVEL_RANK.get(h.get("level", "none"), 0)

    main = max(hazards, key=_rank)
    main_rank = _rank(main)

    watch_rank = _LEVEL_RANK["watch"]
    num_watch_or_more = sum(1 for h in hazards if _rank(h) >= watch_rank)

    overall_rank = main_rank
    if num_watch_or_more >= 2 and overall_rank < len(_LEVEL_ORDER) - 1:
        overall_rank += 1

    overall_level = _LEVEL_ORDER[overall_rank]
    headline = main.get("headline", "Cảnh báo thời tiết")

    others = [h for h in hazards if h is not main and _rank(h) >= watch_rank]
    if not others:
        overall_comment = headline
    else:
        other_types = ", ".join(sorted({h.get("type", "") for h in others if h.get("type")}))
        overall_comment = (
            f"{headline}. Đồng thời có thêm các yếu tố bất lợi: {other_types}, "
            "làm mức cảnh báo tổng thể tăng lên."
        )

    return overall_level, overall_comment


def _build_alerts_from_obs(
    temp_c: Optional[float],
    wind_ms: Optional[float],
    precip_mm: Optional[float],
    cloudcover: Optional[float],
    rel_humidity: Optional[float],
    rain_1h: float,
    rain_3h: float,
    rain_6h: float,
) -> Dict[str, Any]:
    hazards: List[Dict[str, Any]] = []

    rain_h = _build_rain_hazard(rain_1h, rain_3h, rain_6h, precip_now=precip_mm)
    if rain_h:
        hazards.append(rain_h)

    heat_cold_h = _build_heat_cold_hazard(temp_c, wind_ms, cloudcover, rel_humidity)
    if heat_cold_h:
        hazards.append(heat_cold_h)

    wind_h = _build_wind_hazard(wind_ms)
    if wind_h:
        hazards.append(wind_h)

    overall_level, overall_comment = _calc_overall_level_and_comment(hazards)
    return {"overall_level": overall_level, "overall_comment": overall_comment, "hazards": hazards}


# =============================================================================
# 3) DB HELPERS (rain accum + snapshot selection)
# =============================================================================

def _floor_to_hour_utc(dt):
    return dt.astimezone(dt_timezone.utc).replace(minute=0, second=0, microsecond=0)


def _sum_rain_in_window(rows: List[Tuple[Any, Any]], anchor_ts) -> Tuple[float, float, float, float]:
    """
    Từ danh sách (valid_at, precip_mm) đã sort DESC, tính:
    - rain_1h, rain_3h, rain_6h
    - coverage_ratio: tỷ lệ số giờ có dữ liệu / 7 (t, t-1, ..., t-6)
    """
    have_hours = set()
    rain_1h = rain_3h = rain_6h = 0.0

    for valid_at, precip_mm in rows:
        try:
            dt = anchor_ts - valid_at
            hours = dt.total_seconds() / 3600.0
        except Exception:
            continue

        # chỉ tính trong [0, 6]
        if hours < 0 or hours > 6.0:
            continue

        # bucket giờ
        hour_bucket = int(round(hours))  # dữ liệu đúng giờ nên hours ~ integer
        have_hours.add(hour_bucket)

        p = float(precip_mm or 0.0)
        if hours <= 1.0:
            rain_1h += p
            rain_3h += p
            rain_6h += p
        elif hours <= 3.0:
            rain_3h += p
            rain_6h += p
        else:
            rain_6h += p

    coverage_ratio = len(have_hours) / 7.0  # 0..1
    return rain_1h, rain_3h, rain_6h, coverage_ratio


def _fetch_rain_accums(location_id, latest_valid_at, fcst_provider="ML", coverage_threshold=0.7) -> Tuple[float, float, float]:
    """
    Rain accumulation strategy (đồ án chuẩn):
    - Ưu tiên OBS (openmeteo) vì là quan trắc thật.
    - Nếu OBS không đủ coverage trong 6h gần nhất (coverage_ratio < threshold) thì fallback sang FCST (provider=ML).
    """
    if latest_valid_at is None:
        return 0.0, 0.0, 0.0

    # 1) OBS rows
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT w.valid_at, COALESCE(w.precip_mm, 0) AS precip_mm
            FROM public.weather_hourly_obs w
            WHERE w.source = 'openmeteo'
              AND w.location_id = %s
              AND w.valid_at <= %s
              AND w.valid_at >= %s - interval '6 hour'
            ORDER BY w.valid_at DESC;
            """,
            [str(location_id), latest_valid_at, latest_valid_at],
        )
        obs_rows = cur.fetchall()

    if obs_rows:
        r1, r3, r6, cov = _sum_rain_in_window(obs_rows, latest_valid_at)
        if cov >= float(coverage_threshold):
            return r1, r3, r6

    # 2) fallback FCST
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT f.valid_at, COALESCE(f.precip_mm, 0) AS precip_mm
            FROM public.weather_hourly_fcst f
            WHERE f.provider = %s
              AND f.location_id = %s
              AND f.valid_at <= %s
              AND f.valid_at >= %s - interval '6 hour'
            ORDER BY f.valid_at DESC;
            """,
            [fcst_provider, str(location_id), latest_valid_at, latest_valid_at],
        )
        fcst_rows = cur.fetchall()

    if fcst_rows:
        r1, r3, r6, _ = _sum_rain_in_window(fcst_rows, latest_valid_at)
        return r1, r3, r6

    return 0.0, 0.0, 0.0


def _fetch_snapshot_at_hour(location_id, base_utc, fcst_provider="ML") -> Tuple[Optional[Tuple], Optional[str], Optional[str]]:
    """
    Chọn snapshot đúng giờ base_utc theo thứ tự:
      1) OBS (openmeteo) đúng giờ
      2) FCST provider=ML đúng giờ
      3) OBS mới nhất (fallback cuối)

    Returns:
      row, snapshot_source, snapshot_provider
    snapshot_source: "obs" | "fcst"
    snapshot_provider: "openmeteo" | "ML"
    """
    with connection.cursor() as cur:
        # 1) OBS đúng giờ
        cur.execute(
            """
            SELECT
              l.id, l.name, l.lat, l.lon,
              w.valid_at,
              w.temp_c, w.wind_ms, w.precip_mm,
              w.wind_dir_deg,
              w.rel_humidity_pct, w.cloudcover_pct, w.surface_pressure_hpa
            FROM public.locations l
            JOIN public.weather_hourly_obs w ON w.location_id = l.id
            WHERE w.source = 'openmeteo'
              AND l.id = %s
              AND w.valid_at = %s
            LIMIT 1;
            """,
            [str(location_id), base_utc],
        )
        row = cur.fetchone()
        if row:
            return row, "obs", "openmeteo"

        # 2) FCST đúng giờ
        cur.execute(
            """
            SELECT
              l.id, l.name, l.lat, l.lon,
              f.valid_at,
              f.temp_c, f.wind_ms, f.precip_mm,
              f.wind_dir_deg,
              f.rel_humidity_pct, f.cloudcover_pct, f.surface_pressure_hpa
            FROM public.locations l
            JOIN public.weather_hourly_fcst f ON f.location_id = l.id
            WHERE f.provider = %s
              AND l.id = %s
              AND f.valid_at = %s
            LIMIT 1;
            """,
            [fcst_provider, str(location_id), base_utc],
        )
        row = cur.fetchone()
        if row:
            return row, "fcst", fcst_provider

        # 3) OBS mới nhất
        cur.execute(
            """
            SELECT
              l.id, l.name, l.lat, l.lon,
              w.valid_at,
              w.temp_c, w.wind_ms, w.precip_mm,
              w.wind_dir_deg,
              w.rel_humidity_pct, w.cloudcover_pct, w.surface_pressure_hpa
            FROM public.locations l
            JOIN public.weather_hourly_obs w ON w.location_id = l.id
            WHERE w.source = 'openmeteo'
              AND l.id = %s
            ORDER BY w.valid_at DESC
            LIMIT 1;
            """,
            [str(location_id)],
        )
        row = cur.fetchone()
        if row:
            return row, "obs", "openmeteo"

    return None, None, None


# =============================================================================
# 4) API
# =============================================================================

def obs_summary(request, location_id):
    """
    GET /api/obs/summary/<location_id>

    Response JSON giữ nguyên cấu trúc như phiên bản trước (để frontend không phải sửa):
      - found
      - location
      - obs (snapshot current; có thể lấy từ OBS hoặc FCST)
      - today.summary_text
      - current.summary_text
      - alerts { overall_level, overall_comment, hazards[] }

    Bổ sung thêm metadata trong obs:
      - _source: "obs"|"fcst"
      - _provider: "openmeteo"|"ML"
    (frontend không cần dùng, nhưng rất hữu ích cho debug/báo cáo.)
    """
    try:
        UUID(str(location_id))
    except Exception:
        return HttpResponseBadRequest("invalid location_id")

    fcst_provider = "ML"

    now_utc = timezone.now().astimezone(dt_timezone.utc)
    base_utc = _floor_to_hour_utc(now_utc)

    row, snapshot_source, snapshot_provider = _fetch_snapshot_at_hour(location_id, base_utc, fcst_provider=fcst_provider)
    if not row:
        return JsonResponse({"found": False}, status=404)

    (
        loc_id, loc_name, lat, lon,
        valid_at,
        temp_c, wind_ms, precip_mm,
        wind_dir_deg,
        rel_humidity, cloudcover, surface_pressure,
    ) = row

    # Mưa tích lũy: ưu tiên OBS, thiếu coverage -> fallback FCST
    rain_1h, rain_3h, rain_6h = _fetch_rain_accums(
        loc_id,
        valid_at,
        fcst_provider=fcst_provider,
        coverage_threshold=0.7,
    )

    today_text = _build_today_comment(temp_c, precip_mm, cloudcover)
    current_text = _build_current_comment(temp_c, wind_ms, precip_mm)

    alerts = _build_alerts_from_obs(
        temp_c=temp_c,
        wind_ms=wind_ms,
        precip_mm=precip_mm,
        cloudcover=cloudcover,
        rel_humidity=rel_humidity,
        rain_1h=rain_1h,
        rain_3h=rain_3h,
        rain_6h=rain_6h,
    )

    data = {
        "found": True,
        "location": {"id": str(loc_id), "name": loc_name, "lat": float(lat), "lon": float(lon)},
        "obs": {
            "valid_at": valid_at.isoformat(),
            "temp_c": temp_c,
            "wind_ms": wind_ms,
            "precip_mm": precip_mm,
            "wind_dir_deg": wind_dir_deg,
            "rel_humidity_pct": rel_humidity,
            "cloudcover_pct": cloudcover,
            "surface_pressure_hpa": surface_pressure,
            # metadata bổ sung (không phá frontend)
            "_source": snapshot_source,
            "_provider": snapshot_provider,
        },
        "today": {"summary_text": today_text},
        "current": {"summary_text": current_text},
        "alerts": alerts,
    }
    return JsonResponse(data)