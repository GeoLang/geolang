# Debian bookworm base so the QGIS apt packages (whose python bindings are built
# for debian's python3.11) match this image's CPython 3.11.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    TOOL_EXEC_DIR=/app/geolang \
    QT_QPA_PLATFORM=offscreen \
    QGIS_PREFIX_PATH=/usr \
    PATH=/opt/venv/bin:$PATH

# Build tools for the geo stack's C extensions, QGIS, locales, curl for healthchecks
# download.qgis.org and qgis.org answer 503 now and then
RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y \
    wget \
    curl \
    gnupg \
    ca-certificates \
    python3-dev \
    gcc \
    g++ \
    libgeos-dev \
    libproj-dev \
    lsb-release \
    locales \
    && echo "en_US.UTF-8 UTF-8" > /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && update-locale LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 \
    && mkdir -p /etc/apt/keyrings \
    && wget --tries=10 --waitretry=15 --retry-on-http-error=500,502,503,504 -O /etc/apt/keyrings/qgis-archive-keyring.gpg https://download.qgis.org/downloads/qgis-archive-keyring.gpg \
    && chmod a+r /etc/apt/keyrings/qgis-archive-keyring.gpg \
    && printf 'Types: deb deb-src\nURIs: https://qgis.org/debian\nSuites: bookworm\nArchitectures: amd64\nComponents: main\nSigned-By: /etc/apt/keyrings/qgis-archive-keyring.gpg\n' > /etc/apt/sources.list.d/qgis.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y \
        qgis \
        python3-qgis \
        qgis-plugin-grass \
        qgis-providers \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# One venv for the API and the tools: they run in the same process now.
COPY requirements_client.txt requirements.txt /tmp/
RUN python -m venv /opt/venv \
    && pip install --no-cache-dir -r /tmp/requirements_client.txt -r /tmp/requirements.txt \
    && playwright install --with-deps chromium \
    && rm /tmp/requirements_client.txt /tmp/requirements.txt

# SELinux-friendly outputs and writable runtime mount targets
RUN mkdir -p /app/geolang/outputs /app/geolang/user_data /app/geolang/live_data /app/geolang/natural_earth \
    && chown 1000:1000 /app/geolang/outputs /app/geolang/user_data /app/geolang/live_data /app/geolang/natural_earth \
    && chmod 777 /app/geolang/outputs

WORKDIR /app/geolang
COPY src/ ./src/

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://localhost:8080/health || exit 1
CMD ["uvicorn", "src.api.server:app", "--host", "0.0.0.0", "--port", "8080"]
