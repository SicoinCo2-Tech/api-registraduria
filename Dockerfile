FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar aplicación
COPY app.py .

# Variables de entorno
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:10000/health', timeout=5)" || exit 1

# Comando Gunicorn optimizado
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "2", \
     "--threads", "4", \
     "--timeout", "90", \
     "--graceful-timeout", "90", \
     "--keep-alive", "120", \
     "--worker-class", "sync", \
     "--preload", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info"]
