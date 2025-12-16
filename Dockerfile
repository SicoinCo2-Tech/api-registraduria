FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Copiar requirements
COPY requirements.txt .

# Instalar dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar aplicación
COPY app.py .

# Instalar navegadores de Playwright
RUN playwright install chromium
RUN playwright install-deps chromium

# Variables de entorno
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PYTHONUNBUFFERED=1

# Health check mejorado
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 \
  CMD curl -f http://localhost:10000/health || exit 1

# Exponer puerto
EXPOSE 10000

# Comando de inicio - SIN PRELOAD y con menos workers
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "1", \
     "--worker-class", "sync", \
     "--threads", "1", \
     "--timeout", "180", \
     "--graceful-timeout", "180", \
     "--keep-alive", "120", \
     "--log-level", "info", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
