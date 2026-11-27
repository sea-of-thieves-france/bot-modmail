# Image Python épinglée, suivie par Renovate.
FROM python:3.14-slim-bookworm@sha256:e60ad028afdcdd14772a3d46decf0eaf616720f9102aeb4397a2045ff6718c3d AS base

RUN apt-get update &&  \
    apt-get install --no-install-recommends -y \
    # Install CairoSVG dependencies.
    libcairo2 && \
    # Cleanup APT.
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* && \
    # Create a non-root user.
    useradd --shell /usr/sbin/nologin --create-home -d /opt/modmail modmail

FROM base AS builder

COPY requirements.txt .

RUN pip install --root-user-action=ignore --no-cache-dir --upgrade pip wheel && \
    python -m venv /opt/modmail/.venv && \
    . /opt/modmail/.venv/bin/activate && \
    pip install --no-cache-dir --upgrade -r requirements.txt

FROM base

# Copy the entire venv.
COPY --from=builder --chown=modmail:modmail /opt/modmail/.venv /opt/modmail/.venv

# Copy repository files.
WORKDIR /opt/modmail
USER modmail:modmail
COPY --chown=modmail:modmail . .

# This sets some Python runtime variables and disables the internal auto-update.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/modmail/.venv/bin:$PATH \
    USING_DOCKER=yes \
    HEALTH_PORT=8080

# /ready : le conteneur n'est sain qu'une fois Discord, Mongo et S3 joignables.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('HEALTH_PORT', '8080') + '/ready', timeout=4).status == 200 else 1)"]

CMD ["python", "bot.py"]
