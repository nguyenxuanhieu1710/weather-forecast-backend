
<div align="center">
  <img src="https://cdn-icons-png.flaticon.com/512/2165/2165061.png" width="100" />
  <h1>🌪️ Vietnam Weather Forecast Backend</h1>
  <p>
    <b>Core API & Data Processing Unit | Graduation Thesis 2025</b>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python" />
    <img src="https://img.shields.io/badge/Django-092E20?style=for-the-badge&logo=django&logoColor=white" alt="Django" />
    <img src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL" />
    <img src="https://img.shields.io/badge/Celery-37814A?style=for-the-badge&logo=celery&logoColor=white" alt="Celery" />
    <img src="https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=white" alt="Redis" />
  </p>
</div>

---

## 📖 Giới thiệu (Overview)

Đây là **Backend Server** đóng vai trò trái tim của hệ thống "Theo dõi, dự báo và cảnh báo thiên tai tại Việt Nam". Hệ thống này chịu trách nhiệm thu thập dữ liệu khí tượng phức tạp từ các nguồn quốc tế, xử lý tính toán và cung cấp API chuẩn RESTful cho cả Website và Mobile App.

Dự án tập trung vào việc xử lý **dữ liệu không gian (Geospatial Data)** và **dữ liệu chuỗi thời gian (Time-series)** quy mô lớn để đưa ra các dự báo chính xác và cảnh báo sớm thiên tai.

## 🚀 Tính năng chính (Key Features)

### 1. Thu thập dữ liệu tự động (Automated Data Crawling)
* **Nguồn dữ liệu:** Kết nối trực tiếp với máy chủ của **NOAA (National Oceanic and Atmospheric Administration)**.
* **Mô hình:** Sử dụng dữ liệu **GFS (Global Forecast System)** và **GEFS** cho độ chính xác cao.
* **Định dạng:** Xử lý các file dữ liệu khí tượng phức tạp dạng `.nc` (NetCDF) và `.grib2`.
* **Lịch trình:** Tự động chạy tác vụ (Cronjob) cập nhật dữ liệu mới mỗi 6 giờ/lần.

### 2. Bộ xử lý trung tâm (Data Processing Engine)
* **Công nghệ:** Sử dụng `Xarray`, `Pandas` và `NetCDF4` để đọc và nội suy dữ liệu lưới (Grid data).
* **Tính toán:**
    * Tính tổng lượng mưa tích lũy (Precipitation accumulation).
    * Phân tích hướng gió (U-wind, V-wind) và tốc độ gió.
    * Trích xuất nhiệt độ, độ ẩm, áp suất khí quyển.
* **Phủ trùm:** Dữ liệu bao phủ toàn bộ lãnh thổ Việt Nam và vùng Biển Đông.

### 3. Hệ thống cảnh báo (Warning System)
* Phân tích ngưỡng mưa (Thresholds) để đưa ra các mức cảnh báo nguy cơ lũ quét, sạt lở đất.
* Cung cấp API cảnh báo thời gian thực dựa trên vị trí người dùng.

### 4. API Service
* **RESTful API:** Cung cấp endpoints chuẩn cho Frontend và Mobile App.
* **Geo-Query:** Hỗ trợ truy vấn dữ liệu thời tiết theo tọa độ GPS (Latitude/Longitude).

## 🛠️ Công nghệ sử dụng (Tech Stack)

| Component | Technology | Description |
| :--- | :--- | :--- |
| **Language** | Python 3.9+ | Ngôn ngữ xử lý chính. |
| **Framework** | Django & DRF | Xây dựng API và quản trị hệ thống. |
| **Database** | PostgreSQL + PostGIS | Lưu trữ dữ liệu không gian (GIS) và Time-series. |
| **Async Tasks** | Celery + Redis | Xử lý tác vụ nền (Background jobs) và hàng đợi. |
| **Data Science** | Pandas, Numpy, Xarray | Thư viện tính toán khoa học và xử lý mảng nhiều chiều. |

## 📂 Cấu trúc thư mục (Folder Structure)

```bash
weather-forecast-backend/
├── api/                  # Các API Views & Serializers
├── core/                 # Logic xử lý dữ liệu chính (Crawl, Process)
├── data/                 # Thư mục lưu tạm file NetCDF/GRIB tải về
├── weather_backend/      # Cấu hình dự án (Settings, URLs)
├── manage.py             # File điều khiển Django
├── requirements.txt      # Danh sách thư viện phụ thuộc
└── README.md             # Tài liệu hướng dẫn

```

## ⚙️ Hướng dẫn cài đặt (Installation)

Để chạy backend ở môi trường local, vui lòng thực hiện các bước sau:

**Bước 1: Clone dự án**

```bash
git clone [https://github.com/nguyenxuanhieu1710/weather-forecast-backend.git](https://github.com/nguyenxuanhieu1710/weather-forecast-backend.git)
cd weather-forecast-backend

```

**Bước 2: Tạo môi trường ảo (Virtual Environment)**

```bash
# Windows
python -m venv venv
.\venv\Scripts\activate

# MacOS/Linux
python3 -m venv venv
source venv/bin/activate

```

**Bước 3: Cài đặt thư viện**
*Lưu ý: Máy cần cài đặt sẵn GDAL và PROJ (thư viện C++ cho GIS).*

```bash
pip install -r requirements.txt

```

**Bước 4: Cấu hình Database**

1. Tạo Database trong PostgreSQL.
2. Kích hoạt PostGIS extension: `CREATE EXTENSION postgis;`
3. Cập nhật thông tin DB trong file `settings.py` hoặc `.env`.

**Bước 5: Chạy Server**

```bash
python manage.py migrate
python manage.py runserver

```

Server sẽ hoạt động tại: `http://127.0.0.1:8000`

---

## 🤝 Tác giả (Author)

* **Ngọ Đức Duy** - *Backend Developer*
* **Đồ án tốt nghiệp 2025** - Học viện Công nghệ Bưu chính Viễn thông (PTIT)

<div align="center">
Give a ⭐️ if you found this project helpful!
</div>

```

```
