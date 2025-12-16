FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Instalar chromium y dependencias
RUN playwright install chromium
RUN playwright install-deps chromium

# Variables de entorno para Playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "1", \
     "--threads", "2", \
     "--timeout", "180", \
     "--worker-class", "sync", \
     "--max-requests", "100", \
     "--max-requests-jitter", "10"]
