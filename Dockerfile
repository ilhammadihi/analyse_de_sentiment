# Image de base Playwright : Python + Chrome + toutes les dépendances système
# des navigateurs déjà installées (nécessaire pour Trustpilot / Google Maps).
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Dépendances Python (couche cachée tant que requirements ne change pas)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Navigateur Chromium pour Playwright (redondant avec l'image mais garantit la
# version) — léger si déjà présent.
RUN python -m playwright install chromium

# Code applicatif
COPY reviews/ ./reviews/
COPY migrations/ ./migrations/
COPY tools/ ./tools/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Par défaut : l'API. Surchargé par le service `worker` dans docker-compose.
EXPOSE 8000
CMD ["python", "-m", "reviews", "serve"]
