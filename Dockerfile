# Image already contains Chromium + all system libs; version must match requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PORT=10000
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
