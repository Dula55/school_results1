# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.13.3

FROM python:${PYTHON_VERSION}-slim

LABEL fly_launch_runtime="flask"

# Install system dependencies needed for building some Python packages
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /code

# Copy requirements first for better layer caching
COPY requirements.txt requirements.txt

# Install Python dependencies
RUN pip3 install --no-cache-dir -r requirements.txt

# Create necessary directories for persistent storage
# This matches the paths used in app.py for Render deployment
RUN mkdir -p /opt/render/project/data/flask_session \
    && mkdir -p /opt/render/project/data \
    && chmod -R 777 /opt/render/project/data

# Copy application code
COPY . .

# Set environment variables for production
ENV FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    # This will be overridden by Render's environment variables
    RENDER=true

# Expose the port the app runs on
EXPOSE 8080

# Create a non-root user to run the app
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /code /opt/render/project/data

# Switch to non-root user
USER appuser

# Health check to verify the application is running
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Use gunicorn as the production WSGI server
# Note: The app uses PORT environment variable which Render sets automatically
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 4 --threads 2 --timeout 120 'app:app'"]