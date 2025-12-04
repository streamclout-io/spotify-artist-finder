# Python backend Dockerfile
FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for psycopg2
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port for FastAPI
EXPOSE 8000

# Default command (can be overridden in docker-compose)
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
