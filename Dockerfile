# Image Python épinglée, suivie par Renovate.
FROM python:3.15-slim-bookworm@sha256:498829c717b1e59fc42f0e7865eafe4e04ca0f70a3769d4ae70ba218df54507c AS base

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
    USING_DOCKER=yes

CMD ["python", "bot.py"]
