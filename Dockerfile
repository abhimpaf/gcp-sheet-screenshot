FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# We added --timeout 300 to give Playwright 5 minutes to generate all images
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "300", "main:app"]