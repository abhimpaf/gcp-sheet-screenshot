FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Unbuffered stdout so log lines reach Cloud Logging as they happen instead of
# being lost in the buffer when a worker dies.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

# Install the report font into the image so a render never has to reach
# fonts.googleapis.com. Non-fatal: if the package is unavailable the CSS falls
# back to DejaVu Sans Mono, which ships with the base image.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends fonts-ibm-plex \
        || echo "WARNING: fonts-ibm-plex unavailable, falling back to DejaVu Sans Mono") \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fail the build - not the container at 3am - if main.py cannot be imported.
RUN python -c "import main; assert main.app, 'no app'" && echo "import check passed"

# --preload imports the app in the master process, so an import failure prints a
# real traceback instead of gunicorn's bare "App failed to load".
# --threads 2 matches MAX_CONCURRENT_RENDERS: each thread keeps its own Chromium
# (~500MB), so raising this without raising --memory will get the container
# OOM-killed. --timeout 300 gives a large batch room to finish.
CMD exec gunicorn \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --threads 2 \
    --timeout 300 \
    --graceful-timeout 30 \
    --preload \
    --log-level "${LOG_LEVEL:-info}" \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    main:app
