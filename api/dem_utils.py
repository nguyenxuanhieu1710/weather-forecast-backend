# api/dem_utils.py
"""
Tiện ích DEM:
  - sample_elevation(lat, lon): cao độ tuyệt đối (m)
  - sample_relief_local(lat, lon, half_size_px=15): độ cao tương đối so với vùng xung quanh (m)

Ý tưởng relief_local:
  - Từ DEM, lấy 1 cửa sổ nhỏ quanh điểm (khoảng 31x31 pixel nếu half_size_px=15)
  - Tìm MIN cao độ trong cửa sổ (bỏ nodata)
  - relief = elevation_center - local_min
  - relief nhỏ → điểm nằm gần đáy vùng trũng / thung lũng → dễ ngập hơn
"""

import os
import math
from typing import Optional

# Thử import rasterio + Transformer, nếu thiếu thì để None
try:
    import rasterio
    from pyproj import Transformer
except ModuleNotFoundError:
    rasterio = None
    Transformer = None

# Đường dẫn DEM – bạn đang dùng file SRTM_30_VN_UTM.tif cố định
DEM_PATH = (
    os.getenv("DEM_RASTER_PATH")
    or r"C:/Users/ronal/OneDrive/Desktop/weather-backend/backend/data/SRTM_30_VN_UTM.tif"
)

_dem_dataset = None
_dem_transformer: Optional["Transformer"] = None


def _get_dem():
    """
    Mở DEM và chuẩn bị transformer từ WGS84 (lat/lon) sang CRS của DEM.
    Cache lại để không mở file nhiều lần.

    Nếu thiếu rasterio/pyproj hoặc thiếu file DEM -> raise RuntimeError
    (CHỈ khi hàm này được gọi, không phải lúc import module).
    """
    global _dem_dataset, _dem_transformer

    if rasterio is None or Transformer is None:
        raise RuntimeError(
            "Thiếu thư viện rasterio hoặc pyproj, không thể đọc DEM. "
            "Cài thêm package 'rasterio' và 'pyproj' trong venv để dùng tính năng lũ lụt."
        )

    if _dem_dataset is None or _dem_transformer is None:
        if not os.path.exists(DEM_PATH):
            raise RuntimeError(f"DEM file không tồn tại: {DEM_PATH}")

        ds = rasterio.open(DEM_PATH)

        if ds.crs is None:
            raise RuntimeError("DEM không có CRS, kiểm tra lại file DEM / metadata.")

        _dem_dataset = ds
        _dem_transformer = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)

    return _dem_dataset, _dem_transformer


def _sample_single(ds, x: float, y: float) -> Optional[float]:
    """
    Lấy 1 giá trị duy nhất từ raster ds tại tọa độ (x, y) trong CRS của raster.
    """
    for vals in ds.sample([(x, y)]):
        try:
            val = float(vals[0])
        except Exception:
            return None

        if not math.isfinite(val):
            return None

        if ds.nodata is not None and val == ds.nodata:
            return None

        return val

    return None


def sample_elevation(lat: float, lon: float) -> Optional[float]:
    """
    Lấy cao độ (m) tại lat/lon (WGS84).

    Trả về:
      - float: cao độ
      - None : nếu pixel không hợp lệ / ra ngoài phạm vi DEM
    """
    ds, transformer = _get_dem()

    # rasterio dùng thứ tự (x, y) = (lon, lat) nhưng trong CRS của DEM
    x, y = transformer.transform(lon, lat)

    return _sample_single(ds, x, y)


def sample_relief_local(lat: float, lon: float, half_size_px: int = 15) -> Optional[float]:
    """
    Độ cao tương đối so với vùng xung quanh tại lat/lon (WGS84).

    Cách làm:
      - Chuyển lat/lon sang (row, col) trong DEM.
      - Lấy window kích thước (2*half_size_px+1) x (2*half_size_px+1) quanh (row, col).
      - Tìm min trong window (bỏ nodata, bỏ giá trị không hợp lệ).
      - relief = elev_center - min_window, clamp >= 0.

    half_size_px = 15 -> cửa sổ 31x31 pixel.
    Với ~120 điểm, đọc như thế cực nhẹ.
    """
    ds, transformer = _get_dem()
    x, y = transformer.transform(lon, lat)

    # Lấy index pixel gần nhất
    row, col = ds.index(x, y)

    # Nếu ra ngoài raster thì trả None
    if row < 0 or row >= ds.height or col < 0 or col >= ds.width:
        return None

    # Tính phạm vi window, clamp vào [0, height), [0, width)
    row_start = max(0, row - half_size_px)
    row_stop = min(ds.height, row + half_size_px + 1)
    col_start = max(0, col - half_size_px)
    col_stop = min(ds.width, col + half_size_px + 1)

    # Đọc patch nhỏ, không cần numpy hay Window object
    window = ((row_start, row_stop), (col_start, col_stop))
    arr = ds.read(1, window=window)
    nodata = ds.nodata

    # Tìm local_min trong patch
    local_min = None
    h = arr.shape[0]
    w = arr.shape[1]

    for r in range(h):
        for c in range(w):
            v = float(arr[r, c])
            if not math.isfinite(v):
                continue
            if nodata is not None and v == nodata:
                continue
            if local_min is None or v < local_min:
                local_min = v

    if local_min is None:
        return None

    # Giá trị tại tâm (row, col) tương ứng với (center_r, center_c)
    center_r = row - row_start
    center_c = col - col_start

    if not (0 <= center_r < h and 0 <= center_c < w):
        return None

    elev_center = float(arr[center_r, center_c])

    if not math.isfinite(elev_center):
        return None
    if nodata is not None and elev_center == nodata:
        return None

    relief = elev_center - local_min
    if relief < 0:
        relief = 0.0

    return relief
