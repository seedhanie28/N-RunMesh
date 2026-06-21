FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 nrunmesh \
    && chown -R nrunmesh:nrunmesh /app

USER nrunmesh

EXPOSE 3012

CMD ["sh", "-c", "python -m app.bootstrap && exec gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:3012 app.main:app --timeout 120 --access-logfile - --error-logfile -"]
