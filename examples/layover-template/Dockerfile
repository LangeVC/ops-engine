FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml and install dependencies first (for caching)
COPY pyproject.toml /app/
RUN pip install --no-cache-dir .

# Copy project files
COPY . /app/

# Expose FastAPI port
EXPOSE 8000

# Bind inside the container only; the Docker network, not the host, carries the
# ingress. The host port is not published (see docker-compose.yml), so the
# endpoints are reachable from a reverse proxy on the same network, never from
# 0.0.0.0 on the host.
CMD ["uvicorn", "src.main:app", "--host", "127.0.0.1", "--port", "8000"]
