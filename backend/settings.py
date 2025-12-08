import os
from pathlib import Path
import dj_database_url
import environ

# === Paths ===
BASE_DIR = Path(__file__).resolve().parent.parent  # -> thư mục gốc (chứa manage.py)

# === Env ===
env = environ.Env()
# Đọc .env ngay tại gốc project (KHÔNG dùng BASE_DIR.parent)
env.read_env(os.path.join(BASE_DIR, ".env"))

# === Core ===
SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-secret")
DEBUG = env.bool("DEBUG", default=True)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["*"])

# === Installed apps ===
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # KHÔNG dùng GeoDjango khi chạy local để khỏi cần GDAL/GEOS
    # "django.contrib.gis",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "corsheaders",
    # local
    "api",
]

# === Middleware ===
MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "backend.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "backend.wsgi.application"

# === Database (local, không Docker) ===
# .env cần có: DATABASE_URL=postgresql://postgres:PASS@HOST:5432/postgres?sslmode=require
DB_URL_RAW = env("DATABASE_URL", default="")
if DB_URL_RAW:
    DB_URL = DB_URL_RAW.strip().strip('"').strip("'")
    DATABASES = {"default": dj_database_url.parse(DB_URL, conn_max_age=600)}
    # KHÔNG dùng PostGIS engine khi chạy local để tránh GDAL:
    DATABASES["default"]["ENGINE"] = "django.db.backends.postgresql"
else:
    # fallback (nếu chưa có DATABASE_URL) → cho phép chạy trang admin/health
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# === Static files ===
STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# === Timezone ===
TIME_ZONE = "Asia/Bangkok"
USE_TZ = True

# === DRF ===
REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# === Django defaults ===
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Prod: tắt dòng trên và dùng danh sách origin cụ thể (http/https + host + port)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[
    # "http://localhost:3000",
    # "http://192.168.1.60:5173",
    # "https://frontend.example.com",
])

# Nếu dùng cookie/session hoặc Authorization kèm credentials từ trình duyệt:
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=False)

# === CORS / CSRF ===
# Dev: mở hết cho dễ test
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=True)

# Prod: tắt dòng trên và dùng danh sách origin cụ thể (http/https + host + port)
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[
    # "http://localhost:3000",
    # "http://192.168.1.60:5173",
    # "https://frontend.example.com",
])

# Nếu dùng cookie/session hoặc Authorization kèm credentials từ trình duyệt:
CORS_ALLOW_CREDENTIALS = env.bool("CORS_ALLOW_CREDENTIALS", default=False)

# CSRF (bắt buộc có scheme + host + port nếu không 80/443)
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[
    # "http://192.168.1.60:3000",
    # "https://frontend.example.com",
])
