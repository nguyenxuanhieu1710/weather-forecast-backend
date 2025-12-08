from django.urls import path
from .views_obs import (
    latest_snapshot,
    merged_timeseries,
    nearest_point,
    rain_frames,
)
from .views_alerts import obs_summary
from .views_overview import obs_overview
from . import views_flood
from . import views_fcst

urlpatterns = [
    path("obs/latest", latest_snapshot),
    path("obs/timeseries/<uuid:location_id>", merged_timeseries),
    path("obs/nearest", nearest_point),
    path("obs/summary/<uuid:location_id>", obs_summary),
    path("obs/overview", obs_overview),
    path("obs/rain_frames", rain_frames),
    path(
        "obs/flood_risk_latest",
        views_flood.flood_risk_latest,
        name="flood_risk_latest",
    ),
    path("fcst/hourly/<uuid:location_id>", views_fcst.hourly_fcst_latest_run),
]
