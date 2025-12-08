# api/models.py
from django.db import models

class Location(models.Model):
    id = models.UUIDField(primary_key=True)
    name = models.TextField(unique=True)
    # Bảng thật có cột geometry, nhưng ta KHÔNG map vào model để khỏi cần GIS.
    lat = models.FloatField()
    lon = models.FloatField()
    tags = models.JSONField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = "locations"

class WeatherHourlyObs(models.Model):
    id = models.UUIDField(primary_key=True)
    location = models.ForeignKey(Location, on_delete=models.CASCADE, db_column="location_id")
    valid_at = models.DateTimeField()
    source = models.TextField(default="openweather")
    temp_c = models.FloatField(null=True, blank=True)
    wind_ms = models.FloatField(null=True, blank=True)
    precip_mm = models.FloatField(null=True, blank=True)
    raw = models.JSONField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = "weather_hourly_obs"
        unique_together = (("location", "valid_at", "source"),)
        indexes = [
            models.Index(fields=["location", "valid_at"], name="idx_obs_loc_time_django"),
        ]
