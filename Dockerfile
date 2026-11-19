# Immagine multi-architettura: amd64 per lo sviluppo, arm64 per la Pi.
# numpy, scipy, matplotlib e confluent-kafka hanno wheel per manylinux aarch64, quindi niente
# compilazione: e' la ragione per cui la base e' Debian slim e non Alpine.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    CALIBRATION_PIPELINE_DIR=/app/pipeline \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Prima le sole dipendenze: lo strato resta in cache finche' uv.lock non cambia.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY pipeline ./pipeline
RUN uv sync --frozen --no-dev

# La versione del motore che il DCC cita in usedSoftware [S §6.2]: la passa la CI.
ARG ENGINE_GIT_SHA=sconosciuto
ENV ENGINE_GIT_SHA=${ENGINE_GIT_SHA}

# Nessun privilegio: il motore legge la pipeline e scrive solo in cartelle temporanee.
# UID e GID sono fissi perche' il secret del client Keycloak e' un file della macchina montato
# cosi' com'e' (Compose senza Swarm non ne cambia il proprietario): sulla macchina lo si assegna
# a 10001 con permessi 600, e solo questo utente lo legge.
RUN groupadd --system --gid 10001 engine && useradd --system --uid 10001 --gid engine --no-create-home engine
USER 10001:10001

# Il consumer tocca un file a ogni giro di poll: se smette (bloccato o caduto) il container
# risulta malato. start-period lascia il tempo di ottenere il token e collegarsi a Kafka.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 CMD ["calibration-engine", "health"]

CMD ["calibration-engine", "consume"]
