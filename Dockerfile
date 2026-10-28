# ---- مرحله ساخت ----
FROM python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install \
    "aiogram>=3.13,<4" "instagrapi>=3.0" "yt-dlp>=2026.10.26" \
    "pydantic>=2.7" "pydantic-settings>=2.3" "requests>=2.31"

# ---- مرحله اجرا ----
FROM python:3.12-slim
# ffmpeg برای فشرده‌سازی ویدیوهای بزرگ لازم است (بخش ۳.۳ سند)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
WORKDIR /app
COPY app ./app
COPY pyproject.toml ./
# دیتای پایدار بیرون از image (بخش ۲۵.۴)
ENV PYTHONUNBUFFERED=1 STORAGE_DIR=/app/storage
RUN mkdir -p /app/storage
VOLUME ["/app/storage"]
# کاربر غیر root برای امنیت
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser
CMD ["python", "-m", "app.main"]
