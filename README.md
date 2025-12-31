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

Dự án tập trung vào việc xử lý dữ liệu không gian (Geospatial Data) và dữ liệu chuỗi thời gian (Time-series) để đưa ra các dự báo chính xác và cảnh báo sớm.

## 🚀 Tính năng chính (Key Features)

* **🌐 Automated Data Crawling:**
    * Tự động thu thập dữ liệu **GFS (Global Forecast System)** và **GEFS** từ máy chủ NOAA (Mỹ) theo chu kỳ 6 giờ/lần.
    * Xử lý các file định dạng phức tạp như `NetCDF`, `GRIB2`.
* **⚙️ Data Processing Engine:**
    * Sử dụng thư viện `Xarray` và `NetCDF4` để nội suy và trích xuất dữ liệu.
    * Tính toán lượng mưa tích lũy, nhiệt độ, độ ẩm, hướng gió và áp suất cho toàn lãnh thổ Việt Nam.
* **⚠️ Disaster Warning System:**
    * Thuật toán phân tích ngưỡng mưa (Precipitation Thresholds) để đưa ra các mức cảnh báo lũ quét và sạt lở đất.
    * Phân vùng rủi ro dựa trên địa hình và lịch sử dữ liệu.
* **📡 RESTful API Service:**
    * Cung cấp endpoints tối ưu cho Frontend (ReactJS) và Mobile (React Native).
    * Hỗ trợ truy vấn dữ liệu theo tọa độ (Lat/Lon) và theo khu vực hành chính.

## 🛠️ Công nghệ sử dụng (Tech Stack)

| Component | Technology | Description |
| :--- | :--- | :--- |
| **Language** | Python 3.9+ | Ngôn ngữ xử lý chính. |
| **Framework** | Django & DRF | Xây dựng API và quản trị hệ thống. |
| **Database** | PostgreSQL + PostGIS | Lưu trữ dữ liệu không gian (GIS) và Time-series. |
| **Async Tasks** | Celery + Redis | Xử lý tác vụ nền (Background jobs) và hàng đợi cào dữ liệu. |
| **Data Science** | Pandas, Numpy, Xarray | Thư viện tính toán khoa học và xử lý mảng nhiều chiều. |

## ⚙️ Cài đặt và Triển khai (Installation)

Để chạy backend ở môi trường local, vui lòng thực hiện các bước sau:

**Bước 1: Clone dự án**
```bash
git clone [https://github.com/nguyenxuanhieu1710/weather-forecast-backend.git](https://github.com/nguyenxuanhieu1710/weather-forecast-backend.git)
cd weather-forecast-backend
