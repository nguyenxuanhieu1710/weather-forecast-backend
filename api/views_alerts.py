# api/views_alerts.py
import json
from uuid import UUID

from django.db import connection
from django.http import JsonResponse, HttpResponseBadRequest


# ================== Helpers đọc raw ==================


def _parse_raw(raw_val):
    """
    raw trong weather_hourly_obs đang lưu JSON kiểu:
      {
        "cloudcover": ...,
        "surface_pressure": ...,
        (tùy dataset có thể có thêm "relative_humidity" / "humidity" / "rh")
      }

    Trả về:
      - cloudcover (%)
      - surface_pressure (hPa)
      - rel_humidity (%) nếu có, else None
    """
    if not raw_val:
        return None, None, None

    try:
        if isinstance(raw_val, dict):
            obj = raw_val
        else:
            obj = json.loads(raw_val)
    except Exception:
        return None, None, None

    cc = obj.get("cloudcover")
    sp = obj.get("surface_pressure")

    # Thử đọc độ ẩm nếu có trong raw
    rh = (
        obj.get("relative_humidity")
        or obj.get("humidity")
        or obj.get("rh")
    )

    try:
        cc = float(cc) if cc is not None else None
    except Exception:
        cc = None

    try:
        sp = float(sp) if sp is not None else None
    except Exception:
        sp = None

    try:
        rh = float(rh) if rh is not None else None
    except Exception:
        rh = None

    return cc, sp, rh


# ================== Summary mô tả (giống code cũ) ==================


def _build_today_comment(temp_c, precip_mm, cloudcover):
    """
    Sinh câu nhận xét ngắn cho phần TODAY'S WEATHER,
    chỉ dùng các biến có trong obs: temp_c, precip_mm, cloudcover.
    """
    parts = []

    # 1) Mưa / không mưa
    if precip_mm is not None and precip_mm >= 1.0:
        parts.append("Có mưa, trời ẩm ướt")
    elif precip_mm is not None and precip_mm >= 0.1:
        parts.append("Mưa rào nhẹ, có thể trơn trượt")
    else:
        # không mưa -> dựa theo mây
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

    # 2) Cảm giác nhiệt
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


def _build_current_comment(temp_c, wind_ms, precip_mm):
    """
    Câu mô tả rất ngắn cho trạng thái hiện tại,
    dùng cho phần CURRENT WEATHER nếu cần.
    """
    pieces = []

    if temp_c is not None:
        pieces.append(f"Nhiệt độ hiện tại khoảng {temp_c:.1f}°C")

    # gió
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

    # mưa
    if precip_mm is not None and precip_mm >= 0.1:
        pieces.append("có mưa")
    elif precip_mm is not None:
        pieces.append("không mưa")

    return ", ".join(pieces)


# ================== Phần ALERTS theo kiểu sản phẩm lớn ==================


# Thứ tự mức độ, dùng chung cho overall
_LEVEL_ORDER = ["none", "info", "watch", "warning", "danger"]
_LEVEL_RANK = {lv: i for i, lv in enumerate(_LEVEL_ORDER)}


def _make_hazard(h_type, level, score, headline, description, advices=None):
    if advices is None:
        advices = []
    # level phải nằm trong _LEVEL_ORDER hoặc ít nhất map được
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


# ---------- MƯA: dùng mưa tích lũy 1–3–6 giờ ----------


def _build_rain_hazard(rain_1h, rain_3h, rain_6h):
    """
    Cảnh báo mưa dựa trên mưa tích lũy 1h, 3h, 6h.
    Ý tưởng:
      - rain_1h: cường độ mưa gần nhất
      - rain_3h + rain_6h: thể hiện mưa kéo dài -> nguy cơ ngập/lũ cao hơn
    """
    if rain_1h is None and rain_3h is None and rain_6h is None:
        return None

    r1 = float(rain_1h or 0.0)
    r3 = float(rain_3h or 0.0)
    r6 = float(rain_6h or 0.0)

    # Chỉ số mưa hiệu dụng, tránh đếm trùng:
    #  - phần 1h gần nhất tính full
    #  - phần 1–3h trước đó nhân 0.6
    #  - phần 3–6h trước đó nhân 0.4
    extra_3h = max(0.0, r3 - r1)
    extra_6h = max(0.0, r6 - r3)
    eff = r1 + 0.6 * extra_3h + 0.4 * extra_6h

    if eff < 0.5:
        # dưới 0.5mm mưa tích lũy -> coi như không đáng kể
        return None

    # Phân loại mưa dựa trên eff (mm) ~ mưa nhẹ/vừa/to
    # Ngưỡng tham khảo từ phân loại mưa thường dùng:
    #   < 5  : mưa nhỏ
    #   5–15 : mưa vừa
    #   15–30: mưa to
    #   > 30 : mưa rất to / nguy hiểm
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

    return _make_hazard("heavy_rain", level, score, headline, desc, adv)


# ---------- NÓNG / LẠNH: dùng Heat Index + Windchill nếu có ----------


def _compute_heat_index(temp_c, rel_humidity):
    """
    Tính xấp xỉ Heat Index (°C) từ nhiệt độ (°C) và độ ẩm tương đối (%).
    Nếu thiếu độ ẩm, trả về temp_c (coi như nhiệt độ cảm nhận ~ nhiệt độ thực).
    Công thức đơn giản hóa từ NOAA, dùng cho T >= 27°C, RH >= 40%.
    """
    if temp_c is None:
        return None

    T = float(temp_c)
    if rel_humidity is None:
        return T

    RH = float(rel_humidity)
    if RH < 0 or RH > 100:
        RH = max(0.0, min(100.0, RH))

    # Nếu nhiệt độ thấp hoặc độ ẩm thấp, heat index ~ T
    if T < 27 or RH < 40:
        return T

    # Công thức hiển thị dạng °C (đã chuyển đổi)
    # Đây là phiên bản xấp xỉ, đủ để phân loại mức độ.
    HI = (
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

    return HI


def _compute_windchill(temp_c, wind_ms):
    """
    Tính Windchill (°C) – nhiệt độ cảm nhận được do gió.
    Áp dụng cho T <= 10°C và gió > ~5 km/h.
    """
    if temp_c is None or wind_ms is None:
        return None

    T = float(temp_c)
    V_kmh = float(wind_ms) * 3.6

    if T > 10 or V_kmh < 5:
        return None

    # Công thức Windchill (Canada) với T tính bằng °C, V km/h
    WC = (
        13.12
        + 0.6215 * T
        - 11.37 * (V_kmh ** 0.16)
        + 0.3965 * T * (V_kmh ** 0.16)
    )

    return WC


def _build_heat_cold_hazard(temp_c, wind_ms, cloudcover, rel_humidity):
    """
    Cảnh báo nóng / lạnh:
      - Lạnh: ưu tiên xét Windchill (T + gió)
      - Nóng: ưu tiên xét Heat Index (T + độ ẩm)
    Nếu không đủ dữ liệu (thiếu RH/gió), fallback về ngưỡng T giống code cũ.
    """
    if temp_c is None:
        return None

    T = float(temp_c)

    # --------- LẠNH / GIÓ RÉT ----------
    windchill = _compute_windchill(T, wind_ms)
    effective_cold = windchill if windchill is not None else T

    if effective_cold <= 15:
        # Chia mức theo hiệu ứng lạnh
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
            desc = (
                f"Nhiệt độ khoảng {T:.1f}°C, "
                "khá mát hoặc hơi lạnh về đêm và sáng sớm."
            )

        adv = [
            "Chuẩn bị áo ấm khi ra ngoài, đặc biệt vào tối và sáng sớm.",
            "Giữ ấm cho trẻ nhỏ, người cao tuổi.",
        ]
        return _make_hazard("cold", level, score, headline, desc, adv)

    # --------- NẮNG NÓNG / NHIỆT ----------
    # Nếu không nóng thì thôi
    if T < 32:
        return None

    HI = _compute_heat_index(T, rel_humidity)
    effective_heat = HI if HI is not None else T

    # Dựa trên Heat Index (°C), ngưỡng tham khảo từ NOAA:
    #   32–41: cẩn trọng
    #   41–54: cẩn trọng cao
    #   > 54 : nguy hiểm
    if effective_heat < 32:
        return None

    # Cho cảm giác: nhiều mây giảm bớt nắng trực tiếp
    cloudy = cloudcover is not None and cloudcover >= 60

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


# ---------- GIÓ: phân loại gần với thang Beaufort ----------


def _build_wind_hazard(wind_ms):
    """
    Cảnh báo gió mạnh dựa trên tốc độ gió hiện tại, map xấp xỉ theo Beaufort.
    """
    if wind_ms is None:
        return None

    w = float(wind_ms)
    wind_kmh = w * 3.6

    # Dưới ~20 km/h: gió nhẹ, không cảnh báo
    if wind_kmh < 20:
        return None

    # Tham khảo Beaufort:
    #  4 BFT ~ 20–28 km/h: gió vừa
    #  5–6 BFT ~ 29–49 km/h: gió mạnh
    #  >= 7 BFT ~ >= 50 km/h: gió rất mạnh
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


# ---------- KẾT HỢP HAZARD TỔNG HỢP ----------


def _calc_overall_level_and_comment(hazards):
    """
    Từ danh sách hazards, xác định mức độ tổng thể và câu nhận xét gộp.

    Logic multi-hazard:
      - Lấy hazard có level cao nhất làm 'main'
      - Nếu có từ 2 hazard trở lên ở mức >= 'watch' -> nâng overall thêm 1 bậc
    """
    if not hazards:
        return (
            "none",
            "Thời tiết nhìn chung ổn định, không có nguy cơ đáng kể trong giờ gần nhất.",
        )

    # hazard chính: level cao nhất
    def _rank(h):
        return _LEVEL_RANK.get(h.get("level", "none"), 0)

    main = max(hazards, key=_rank)
    main_rank = _rank(main)

    # Đếm số hazard ở mức từ watch trở lên
    watch_rank = _LEVEL_RANK["watch"]
    num_watch_or_more = sum(1 for h in hazards if _rank(h) >= watch_rank)

    # Nếu có >=2 yếu tố cùng ở mức watch trở lên -> nâng cấp tổng thể lên 1 bậc
    overall_rank = main_rank
    if num_watch_or_more >= 2 and overall_rank < len(_LEVEL_ORDER) - 1:
        overall_rank += 1

    overall_level = _LEVEL_ORDER[overall_rank]
    headline = main["headline"]

    # Xây comment
    others = [
        h
        for h in hazards
        if h is not main and _rank(h) >= watch_rank
    ]

    if not others:
        overall_comment = headline
    else:
        other_types = ", ".join(sorted({h["type"] for h in others}))
        overall_comment = (
            f"{headline}. Đồng thời có thêm các yếu tố bất lợi: {other_types}, "
            "làm mức cảnh báo tổng thể tăng lên."
        )

    return overall_level, overall_comment


def _build_alerts_from_obs(
    temp_c,
    wind_ms,
    precip_mm,
    cloudcover,
    rel_humidity,
    rain_1h,
    rain_3h,
    rain_6h,
):
    """
    Hàm chính: từ obs hiện tại + mưa tích lũy -> danh sách hazard + mức độ tổng thể.
    Đây là trái tim của \"API cảnh báo\".
    """
    hazards = []

    rain_h = _build_rain_hazard(rain_1h, rain_3h, rain_6h)
    if rain_h:
        hazards.append(rain_h)

    heat_cold_h = _build_heat_cold_hazard(temp_c, wind_ms, cloudcover, rel_humidity)
    if heat_cold_h:
        hazards.append(heat_cold_h)

    wind_h = _build_wind_hazard(wind_ms)
    if wind_h:
        hazards.append(wind_h)

    overall_level, overall_comment = _calc_overall_level_and_comment(hazards)

    return {
        "overall_level": overall_level,
        "overall_comment": overall_comment,
        "hazards": hazards,
    }


# ================== Helpers DB: mưa tích lũy 1–3–6h ==================


def _fetch_rain_accums(location_id, latest_valid_at):
    """
    Lấy mưa tích lũy 1h / 3h / 6h trước thời điểm latest_valid_at cho 1 location.
    """
    if latest_valid_at is None:
        return 0.0, 0.0, 0.0

    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT
              w.valid_at,
              COALESCE(w.precip_mm, 0) AS precip_mm
            FROM public.weather_hourly_obs w
            WHERE w.source = 'openmeteo'
              AND w.location_id = %s
              AND w.valid_at <= %s
              AND w.valid_at >= %s - interval '6 hour'
            ORDER BY w.valid_at DESC;
            """,
            [str(location_id), latest_valid_at, latest_valid_at],
        )
        rows = cur.fetchall()

    rain_1h = 0.0
    rain_3h = 0.0
    rain_6h = 0.0

    for valid_at, precip_mm in rows:
        p = float(precip_mm or 0.0)
        dt = latest_valid_at - valid_at
        hours = dt.total_seconds() / 3600.0

        # hours >=0 vì valid_at <= latest_valid_at
        if hours <= 1.0:
            rain_1h += p
            rain_3h += p
            rain_6h += p
        elif hours <= 3.0:
            rain_3h += p
            rain_6h += p
        elif hours <= 6.0:
            rain_6h += p

    return rain_1h, rain_3h, rain_6h


# ================== API chính ==================


def obs_summary(request, location_id):
    """
    Tóm tắt obs mới nhất + nhận xét + cảnh báo cấu trúc.
    Dùng cho phần TODAY'S WEATHER + ALERTS của 1 điểm.

    GET /api/obs/summary/<location_id>
    """
    try:
        UUID(str(location_id))
    except Exception:
        return HttpResponseBadRequest("invalid location_id")

    # Lấy obs mới nhất cho location_id
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT
              l.id,
              l.name,
              l.lat,
              l.lon,
              w.valid_at,
              w.temp_c,
              w.wind_ms,
              w.precip_mm,
              w.raw
            FROM public.locations l
            JOIN public.weather_hourly_obs w
              ON w.location_id = l.id
            WHERE w.source = 'openmeteo'
              AND l.id = %s
            ORDER BY w.valid_at DESC
            LIMIT 1;
            """,
            [str(location_id)],
        )
        row = cur.fetchone()

    if not row:
        return JsonResponse({"found": False}, status=404)

    (
        loc_id,
        loc_name,
        lat,
        lon,
        valid_at,
        temp_c,
        wind_ms,
        precip_mm,
        raw_val,
    ) = row

    cloudcover, surface_pressure, rel_humidity = _parse_raw(raw_val)

    # Mưa tích lũy 1–3–6 giờ cho location này
    rain_1h, rain_3h, rain_6h = _fetch_rain_accums(loc_id, valid_at)

    today_text = _build_today_comment(temp_c, precip_mm, cloudcover)
    current_text = _build_current_comment(temp_c, wind_ms, precip_mm)
    alerts = _build_alerts_from_obs(
        temp_c,
        wind_ms,
        precip_mm,
        cloudcover,
        rel_humidity,
        rain_1h,
        rain_3h,
        rain_6h,
    )

    data = {
        "found": True,
        "location": {
            "id": str(loc_id),
            "name": loc_name,
            "lat": float(lat),
            "lon": float(lon),
        },
        "obs": {
            "valid_at": valid_at.isoformat(),
            "temp_c": temp_c,
            "wind_ms": wind_ms,
            "precip_mm": precip_mm,
            "cloudcover_pct": cloudcover,
            "surface_pressure_hpa": surface_pressure,
            # rel_humidity không đưa ra để giữ schema cũ, dùng nội bộ cho cảnh báo
        },
        "today": {
            # Dùng cho block TODAY'S WEATHER
            "summary_text": today_text,
        },
        "current": {
            # Dùng cho block CURRENT WEATHER
            "summary_text": current_text,
        },
        "alerts": alerts,
    }

    return JsonResponse(data)
