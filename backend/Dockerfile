FROM python:3.11-slim

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
      poppler-utils \
      tesseract-ocr \
      tesseract-ocr-fra \
      ocrmypdf \
      curl \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Audit 2026-11 : le backend ne tourne plus en root. /app/uploads est créé ici pour que le
# volume nommé hérite des droits de l'utilisateur applicatif au premier montage.
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
  && mkdir -p /app/uploads \
  && chown -R app:app /app
USER app

EXPOSE 8000
# --proxy-headers : l'IP cliente (limitation de débit) vient de X-Forwarded-For posé par nginx,
# qui l'écrase avec l'IP réelle ; le port 8000 n'est joignable que par le proxy (voir compose).
ENV FORWARDED_ALLOW_IPS="*"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
