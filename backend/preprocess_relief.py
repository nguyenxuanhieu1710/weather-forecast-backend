# backend/preprocess_relief.py
import os

import numpy as np
import rasterio
from rasterio.transform import Affine
from scipy.ndimage import minimum_filter

# Đường dẫn DEM gốc
DEM_PATH = os.getenv("DEM_RASTER_PATH") or r"C:/Users/ronal/OneDrive/Desktop/weather-backend/backend/data/SRTM_30_VN_UTM.tif"

# Raster output chứa "độ cao tương đối so với vùng xung quanh"
RELIEF_PATH = os.getenv("RELIEF_RASTER_PATH") or os.path.join(
    os.path.dirname(DEM_PATH),
    "RELIEF_LOCAL_UTM.tif",
)

# Kích thước cửa sổ lân cận (pixel). 21 ~ vùng lân cận khoảng vài km tùy độ phân giải DEM.
WINDOW_SIZE = 21  # phải là số lẻ: 9, 11, 21, ...

def main():
    print(f"[RELIEF] Loading DEM from: {DEM_PATH}")
    with rasterio.open(DEM_PATH) as src:
        dem = src.read(1).astype(np.float32)
        profile = src.profile.copy()
        nodata = src.nodata

    print(f"[RELIEF] DEM shape: {dem.shape}, nodata={nodata}")

    # Xử lý nodata: gán giá trị rất lớn để không trở thành min trong cửa sổ
    if nodata is not None:
        dem_proc = np.where(dem == nodata, 1e6, dem)
    else:
        dem_proc = dem

    print(f"[RELIEF] Computing local minimum with window size = {WINDOW_SIZE}...")
    local_min = minimum_filter(
        dem_proc,
        size=WINDOW_SIZE,
        mode="nearest",
    )

    print("[RELIEF] Computing local relief (height above local minimum)...")
    relief = dem - local_min  # m

    # Nơi là nodata trong DEM thì giữ là nodata trong relief
    if nodata is not None:
        relief[dem == nodata] = nodata

    # Có thể cắt ngưỡng relief để tránh giá trị cực đoan
    relief = np.where(relief < 0, 0, relief)  # không cho âm

    # Cập nhật profile cho output
    profile.update(
        dtype=rasterio.float32,
        nodata=nodata,
        count=1,
        compress="lzw",
    )

    print(f"[RELIEF] Saving relief raster to: {RELIEF_PATH}")
    with rasterio.open(RELIEF_PATH, "w", **profile) as dst:
        dst.write(relief.astype(np.float32), 1)

    print("[RELIEF] Done.")

if __name__ == "__main__":
    main()
