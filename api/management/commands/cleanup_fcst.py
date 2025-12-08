# api/management/commands/cleanup_fcst.py
from datetime import timedelta, timezone as dt_timezone

from django.core.management.base import BaseCommand
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Dọn rác weather_hourly_fcst: giữ tương lai, và trong 48h quá khứ chỉ giữ giờ OBS bị thiếu."

    def handle(self, *args, **options):
        # Thời điểm hiện tại (UTC, floored về đầu phút cho gọn, không bắt buộc)
        now_utc = timezone.now().astimezone(dt_timezone.utc).replace(second=0, microsecond=0)
        cutoff_past = now_utc - timedelta(hours=48)

        self.stdout.write(f"[cleanup_fcst] now_utc = {now_utc.isoformat()}")
        self.stdout.write(f"[cleanup_fcst] cutoff_past = {cutoff_past.isoformat()}")

        with connection.cursor() as cur:
            # 1) Xóa toàn bộ FCST quá xa hơn 48h trước (cho nhẹ DB)
            cur.execute(
                """
                DELETE FROM public.weather_hourly_fcst
                WHERE valid_at < %s
                """,
                [cutoff_past],
            )
            deleted_old = cur.rowcount
            self.stdout.write(f"[cleanup_fcst] Deleted old fcst (< 48h past): {deleted_old}")

            # 2) Trong đoạn [now-48h, now), nếu giờ nào đã có OBS thì xóa FCST tương ứng
            cur.execute(
                """
                DELETE FROM public.weather_hourly_fcst f
                WHERE f.valid_at >= %s
                  AND f.valid_at < %s
                  AND EXISTS (
                    SELECT 1
                    FROM public.weather_hourly_obs o
                    WHERE o.location_id = f.location_id
                      AND o.valid_at   = f.valid_at
                      AND o.source     = 'openmeteo'
                  )
                """,
                [cutoff_past, now_utc],
            )
            deleted_gap = cur.rowcount
            self.stdout.write(f"[cleanup_fcst] Deleted fcst where OBS exists in last 48h: {deleted_gap}")

        self.stdout.write("[cleanup_fcst] Done.")
