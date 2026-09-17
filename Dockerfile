FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Unbuffered stdout so log lines reach Cloud Logging as they happen instead of
# being lost in the buffer when a worker dies.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fail the build - not the container at 3am - if main.py cannot be imported.
RUN python -c "import main; assert main.app, 'no app'" && echo "import check passed"

# --preload imports the app in the master process, so an import failure prints a
# real traceback instead of gunicorn's bare "App failed to load".
# --timeout 300 gives Playwright 5 minutes to generate all images.
CMD exec gunicorn \
    --bind "0.0.0.0:${PORT:-8080}" \
    --workers 1 \
    --threads 8 \
    --timeout 300 \
    --graceful-timeout 30 \
    --preload \
    --log-level "${LOG_LEVEL:-info}" \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    main:app
