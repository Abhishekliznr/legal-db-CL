# Use an official lightweight Python runtime
FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install basic system utilities & postgres library
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory inside the container
WORKDIR /app

# Copy dependency definition and install Python packages
COPY backend/requirements.txt ./backend/
RUN pip install --no-cache-dir -r backend/requirements.txt

# Install Playwright browser binaries and system dependencies
RUN playwright install --with-deps chromium

# Copy the rest of the application code (backend & frontend)
COPY . .

# Expose port 3000 for FastAPI
EXPOSE 3000

# Set PYTHONPATH to find all backend modules
ENV PYTHONPATH=/app/backend:/app

# Universal command: Works in Dev and Production
CMD ["sh", "-c", "uvicorn api:app --app-dir backend --host 0.0.0.0 --port ${PORT:-3000} --workers ${WORKERS:-1} --proxy-headers --forwarded-allow-ips '*'"]
