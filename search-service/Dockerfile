FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY search-service/ .

EXPOSE 8001

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8001"]