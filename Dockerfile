FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN playwright install chromium
RUN playwright install-deps
CMD ["gunicorn", "app:app",
     "--bind", "0.0.0.0:10000",
     "--workers", "1",
     "--threads", "1",
     "--timeout", "150"]

