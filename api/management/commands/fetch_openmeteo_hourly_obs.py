# api/management/commands/fetch_openmeteo_hourly_obs.py
import os
import time
import math
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter, Retry

from django.core.management.base import BaseCommand
from django.db import connections, transaction

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# số dòng upsert mỗi câu executemany để tránh treo DB
DB_CHUNK_SIZE = 200

SQL_SELECT_LOC = """
select id, lat, lon
from public.locations
where active = true
  and lat between %s and %s
  and lon between %s and %s
order by lat, lon;
"""

# Bảng weather_hourly_obs cần có các cột:
# id uuid, location_id uuid, valid_at timestamptz, source text,
# temp_c double precision, wind_ms double precision, precip_mm double precision,
# wind_dir_deg double precision, rel_humidity_pct double precision,
# cloudcover_pct double precision, surface_pressure_hpa double precision,
# UNIQUE(location_id, valid_at, source)
SQL_UPSERT_HOURLY_OBS = """
insert into public.weather_hourly_obs
(id, location_id, valid_at, source,
 temp_c, wind_ms, precip_mm,
 wind_dir_deg, rel_humidity_pct,
 cloudcover_pct, surface_pressure_hpa)
values (gen_random_uuid(), %s, %s, 'openmeteo',
        %s, %s, %s,
        %s, %s,
        %s, %s)
on conflict (location_id, valid_at, source) do update set
  temp_c               = excluded.temp_c,
  wind_ms              = excluded.wind_ms,
  precip_mm            = excluded.precip_mm,
  wind_dir_deg         = excluded.wind_dir_deg,
  rel_humidity_pct     = excluded.rel_humidity_pct,
  cloudcover_pct       = excluded.cloudcover_pct,
  surface_pressure_hpa = excluded.surface_pressure_hpa;
"""


def parse_iso_utc(s: str) -> datetime:
    """
    Open-Meteo trả "YYYY-MM-DDTHH:00" (UTC nếu timezone=UTC).
    Chuẩn hoá về datetime UTC, phút/giây=0.
    """
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0)


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except Exception:
        return None


def _ensure_9_params(row, bi=None, loc_id=None):
    """
    SQL_UPSERT_HOURLY_OBS có đúng 9 placeholders.
    Nếu row không đúng 9 phần tử -> báo lỗi rõ ràng để biết thừa/thiếu ở đâu.
    """
    if row is None:
        raise ValueError(f"[DB] row is None (batch={bi}, loc={loc_id})")

    if not isinstance(row, (tuple, list)):
        raise ValueError(f"[DB] row type invalid={type(row)} (batch={bi}, loc={loc_id}) row={row!r}")

    if len(row) != 9:
        raise ValueError(
            f"[DB] invalid param length={len(row)} expected=9 (batch={bi}, loc={loc_id}) row={row!r}"
        )

    return tuple(row)


class Command(BaseCommand):
    help = "Nạp hourly observation từ Open-Meteo vào weather_hourly_obs (multi-batch, HTTP song song, DB tuần tự)"

    def add_arguments(self, p):
        p.add_argument("--hours", type=int, default=48, help="số giờ gần nhất cần nạp (mặc định 48)")
        p.add_argument("--bbox", type=str, default="", help="lat_min,lat_max,lon_min,lon_max")
        p.add_argument("--max", type=int, default=0, help="giới hạn số location (0 = all)")
        p.add_argument("--stride", type=int, default=1, help="lấy 1 điểm mỗi N ô lưới (>=1)")
        p.add_argument("--offset", type=int, default=0, help="bỏ qua N điểm đầu")
        p.add_argument("--limit", type=int, default=0, help="giới hạn số điểm sau offset (0=all)")
        p.add_argument("--batch-size", type=int, default=300, help="số điểm xử lý mỗi batch (multi-location)")
        p.add_argument(
            "--workers",
            type=int,
            default=int(os.getenv("FETCH_WORKERS", "4")),
            help="số batch fetch HTTP song song (1–16, default 4)",
        )
        p.add_argument("--sleep", type=float, default=0.0, help="nghỉ giữa các batch (s) nếu cần")
        p.add_argument("--no-refresh-mv", action="store_true", help="không refresh materialized views sau khi nạp")
        p.add_argument(
            "--no-prune",
            action="store_true",
            help="không xoá dữ liệu cũ (mặc định sẽ giới hạn đúng cửa sổ --hours, vd 48h)",
        )

    # Helpers refresh MV
    def _refresh_mv(self, mv: str, concurrently: bool = True):
        sql = f"REFRESH MATERIALIZED VIEW {'CONCURRENTLY ' if concurrently else ''}{mv};"
        with connections["default"].cursor() as cur:
            cur.execute(sql)

    def _try_refresh(self, mv: str):
        try:
            self._refresh_mv(mv, concurrently=True)
            self.stdout.write(f"Refreshed MV (concurrently): {mv}")
        except Exception as e:
            self.stderr.write(f"Concurrent refresh failed for {mv}: {e} -> fallback non-concurrent")
            try:
                self._refresh_mv(mv, concurrently=False)
                self.stdout.write(f"Refreshed MV: {mv}")
            except Exception as e2:
                self.stderr.write(f"Refresh MV failed for {mv} (non-fatal): {e2}")

    def handle(self, *args, **o):
        # --- 1) BBox & cửa sổ thời gian ---
        if o["bbox"]:
            lat_min, lat_max, lon_min, lon_max = map(float, o["bbox"].split(","))
        else:
            lat_min = float(os.getenv("VN_LAT_MIN", "8.0"))
            lat_max = float(os.getenv("VN_LAT_MAX", "23.5"))
            lon_min = float(os.getenv("VN_LON_MIN", "102.0"))
            lon_max = float(os.getenv("VN_LON_MAX", "110.75"))

        hours = max(1, int(o["hours"] or 48))
        now_utc = datetime.now(timezone.utc)
        backfrom = (now_utc - timedelta(hours=hours)).replace(minute=0, second=0, microsecond=0)
        past_days = max(1, min(92, math.ceil(hours / 24)))  # đủ bao 48h

        # --- 2) Lấy danh sách locations ---
        with connections["default"].cursor() as cur:
            cur.execute(SQL_SELECT_LOC, [lat_min, lat_max, lon_min, lon_max])
            locs = cur.fetchall()  # [(id, lat, lon), ...]

        stride = max(1, int(o["stride"] or 1))
        if stride > 1:
            locs = locs[::stride]

        off = max(0, int(o["offset"] or 0))
        lim = int(o["limit"] or 0)
        if off or lim:
            locs = locs[off : (off + lim) if lim > 0 else None]

        if o["max"] > 0:
            locs = locs[: o["max"]]

        total_points = len(locs)
        if total_points == 0:
            self.stdout.write("No locations matched the filter.")
            return

        batch_size = max(1, int(o["batch_size"] or 300))
        total_batches = math.ceil(total_points / batch_size)

        workers = max(1, int(o.get("workers") or 4))
        workers = min(workers, 16)
        workers = min(workers, total_batches)  # không nhiều thread hơn số batch

        self.stdout.write(
            f"Points: {total_points} | batches: {total_batches} | "
            f"batch_size={batch_size} | workers={workers} | "
            f"window={hours}h | backfrom={backfrom.isoformat()}"
        )

        # --- 3) Các biến cần nạp + base params ---
        hourly_params = [
            "temperature_2m",
            "wind_speed_10m",
            "wind_direction_10m",
            "precipitation",
            "relative_humidity_2m",
            "cloudcover",
            "surface_pressure",
        ]
        base_params = {
            "hourly": ",".join(hourly_params),
            "past_days": past_days,
            "timezone": "UTC",
            "windspeed_unit": "ms",
            "precipitation_unit": "mm",
        }

        def make_session():
            sess = requests.Session()
            retries = Retry(
                total=6,
                connect=3,
                read=3,
                backoff_factor=1.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=frozenset(["GET"]),
            )
            sess.mount("https://", HTTPAdapter(max_retries=retries))
            headers = {
                "User-Agent": "vn-weather-ingestor/1.0",
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate, br",
            }
            return sess, headers

        # --- 4) Hàm fetch 1 batch (multi-location, 1 request cho cả batch) ---
        def fetch_batch(bi, batch):
            sess, headers = make_session()
            rows = []

            lat_list = [str(lat) for (_, lat, _) in batch]
            lon_list = [str(lon) for (_, _, lon) in batch]

            params = dict(base_params)
            params["latitude"] = ",".join(lat_list)
            params["longitude"] = ",".join(lon_list)

            print(
                f"[FETCH] batch={bi}/{total_batches} points={len(batch)} "
                f"lat[{len(lat_list)}], lon[{len(lon_list)}]",
                flush=True,
            )

            try:
                resp = sess.get(OPEN_METEO_URL, params=params, headers=headers, timeout=40)
                status = resp.status_code
                print(f"[FETCH]   HTTP status={status}", flush=True)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                print(f"[FETCH]   HTTP/JSON ERROR: {e}", flush=True)
                sess.close()
                return bi, batch, [], e

            # Với multi-location, payload có thể là list hoặc dict
            if isinstance(payload, dict):
                payload_list = [payload]
            elif isinstance(payload, list):
                payload_list = payload
            else:
                print(f"[FETCH]   Unexpected payload type: {type(payload)}", flush=True)
                sess.close()
                return bi, batch, [], None

            if not payload_list:
                print("[FETCH]   Empty payload list", flush=True)
                sess.close()
                return bi, batch, [], None

            # Map index j trong payload_list về batch[j]
            for j, obj in enumerate(payload_list):
                if j >= len(batch):
                    break

                loc_id, lat, lon = batch[j]
                hourly = obj.get("hourly", {}) or {}

                times = hourly.get("time", []) or []
                temps = hourly.get("temperature_2m", []) or []
                winds = hourly.get("wind_speed_10m", []) or []
                precs = hourly.get("precipitation", []) or []

                dirs = hourly.get("wind_direction_10m", []) or []
                hums = hourly.get("relative_humidity_2m", []) or []
                clouds = hourly.get("cloudcover", []) or []
                press = hourly.get("surface_pressure", []) or []

                n = min(len(times), len(temps), len(winds), len(precs))
                if n == 0:
                    print(f"[FETCH]   No core hourly data for loc={loc_id}", flush=True)
                    continue

                for k in range(n):
                    try:
                        tdt = parse_iso_utc(times[k])
                    except Exception as e:
                        print(
                            f"[FETCH]   parse time error: {e} | val={times[k]!r} | loc={loc_id}",
                            flush=True,
                        )
                        continue

                    if tdt < backfrom or tdt > now_utc:
                        continue

                    temp_c = _safe_float(temps[k])
                    wind_ms = _safe_float(winds[k])
                    precip = _safe_float(precs[k])

                    wdir_deg = _safe_float(dirs[k]) if k < len(dirs) else None
                    rh_pct = _safe_float(hums[k]) if k < len(hums) else None
                    cloud_pct = _safe_float(clouds[k]) if k < len(clouds) else None
                    sp_hpa = _safe_float(press[k]) if k < len(press) else None

                    row = (
                        loc_id,    # %s 1
                        tdt,       # %s 2
                        temp_c,    # %s 3
                        wind_ms,   # %s 4
                        precip,    # %s 5
                        wdir_deg,  # %s 6
                        rh_pct,    # %s 7
                        cloud_pct, # %s 8
                        sp_hpa,    # %s 9
                    )

                    # đảm bảo đúng 9 params (nếu không đúng sẽ nổ rõ ràng tại đây)
                    rows.append(_ensure_9_params(row, bi=bi, loc_id=loc_id))

            sess.close()
            return bi, batch, rows, None

        # --- 5) Tạo tasks theo batch ---
        tasks = [(bi, list(batch)) for bi, batch in enumerate(chunks(locs, batch_size), 1)]

        # --- 6) Chạy song song phần FETCH, DB vẫn tuần tự ---
        results = []

        if workers > 1 and len(tasks) > 1:
            self.stdout.write(f"Using workers={workers} for HTTP fetch")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(fetch_batch, bi, batch) for (bi, batch) in tasks]
                for fu in as_completed(futs):
                    try:
                        res = fu.result()
                    except Exception as e:
                        res = (None, None, None, e)
                    results.append(res)
        else:
            self.stdout.write("Running fetch in single-thread mode")
            for (bi, batch) in tasks:
                try:
                    results.append(fetch_batch(bi, batch))
                except Exception as e:
                    results.append((bi, batch, None, e))

        # Sắp xếp lại theo thứ tự batch index để log cho dễ đọc
        results.sort(key=lambda x: (x[0] is None, x[0]))

        ok_batches = 0
        total_rows = 0

        for (bi, batch, rows, err) in results:
            if bi is None:
                self.stderr.write(f"[?/?] ERR batch (no index): {err}")
                continue

            self.stdout.write(f"[{bi}/{total_batches}] START batch | points={len(batch) if batch else 0}")

            if err:
                self.stderr.write(f"[{bi}/{total_batches}] ERR batch: {err}")
                continue

            if not rows:
                self.stdout.write(
                    f"[{bi}/{total_batches}] No rows in window | batch_size={len(batch) if batch else 0}"
                )
                continue

            # (tuỳ chọn) kiểm tra nhanh 1 row đầu để chắc chắn
            _ = _ensure_9_params(rows[0], bi=bi, loc_id=rows[0][0] if rows[0] else None)

            num_rows = len(rows)
            num_chunks = max(1, math.ceil(num_rows / DB_CHUNK_SIZE))

            self.stdout.write(
                f"[{bi}/{total_batches}] INSERT total_rows={num_rows} "
                f"with {num_chunks} DB chunks (chunk_size={DB_CHUNK_SIZE})"
            )

            inserted_here = 0
            chunk_idx = 0

            for sub in chunks(rows, DB_CHUNK_SIZE):
                chunk_idx += 1
                self.stdout.write(
                    f"[{bi}/{total_batches}]   DB chunk {chunk_idx}/{num_chunks} size={len(sub)}"
                )

                with transaction.atomic():
                    with connections["default"].cursor() as cur:
                        cur.executemany(SQL_UPSERT_HOURLY_OBS, sub)

                inserted_here += len(sub)

            self.stdout.write(f"[{bi}/{total_batches}] DONE INSERT rows={inserted_here}")

            ok_batches += 1
            total_rows += inserted_here

            if o["sleep"] > 0:
                time.sleep(o["sleep"])

        self.stdout.write(f"Done. ok_batches={ok_batches}/{total_batches} | total_rows={total_rows}")

        # --- 7) Prune dữ liệu cũ: chỉ giữ đúng cửa sổ --hours ---
        if not o.get("no_prune"):
            cutoff = backfrom
            self.stdout.write(f"[PRUNE] Deleting rows older than {cutoff.isoformat()}...")
            with connections["default"].cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM public.weather_hourly_obs
                    WHERE source = 'openmeteo'
                      AND valid_at < %s
                    """,
                    [cutoff],
                )
                deleted = cur.rowcount or 0
            self.stdout.write(f"[PRUNE] Done. deleted {deleted} rows older than {cutoff.isoformat()}")

        # --- 8) Refresh materialized views sau khi nạp xong ---
        if not o.get("no_refresh_mv"):
            self.stdout.write("[MV] Refreshing MV latest_openmeteo_hourly...")
            self._try_refresh("public.latest_openmeteo_hourly")
            self.stdout.write("[MV] Done refresh latest_openmeteo_hourly")
            # self._try_refresh("public.recent48h_openmeteo")
