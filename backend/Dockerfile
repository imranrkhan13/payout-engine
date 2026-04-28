FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# ✅ FIXED PATHS
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy full backend code
COPY backend/ .

CMD ["sh", "-c", "python manage.py migrate && gunicorn playto.wsgi:application --bind 0.0.0.0:$PORT"]