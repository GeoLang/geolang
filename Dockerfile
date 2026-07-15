# Use the official Letta image (Debian Bookworm-based).
# Pinned to a known-good Letta version — bump intentionally after testing
# (see docs/DESIGN.md "Pin the Letta base image").
FROM letta/letta:0.16.8

# Install dependencies, QGIS, locales + BUILD TOOLS (this fixes pandas C extension error)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    python3-pip \
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
    && wget -O /etc/apt/keyrings/qgis-archive-keyring.gpg https://download.qgis.org/downloads/qgis-archive-keyring.gpg \
    && chmod a+r /etc/apt/keyrings/qgis-archive-keyring.gpg \
    && echo "Types: deb deb-src\nURIs: https://qgis.org/debian\nSuites: bookworm\nArchitectures: amd64\nComponents: main\nSigned-By: /etc/apt/keyrings/qgis-archive-keyring.gpg" > /etc/apt/sources.list.d/qgis.sources \
    && apt-get update \
    && apt-get install -y \
        qgis \
        python3-qgis \
        qgis-plugin-grass \
        qgis-providers \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# SELinux-friendly outputs
RUN mkdir -p /app/geolang/outputs && \
    chmod 777 /app/geolang/outputs && \
    chown -R root:root /app/geolang

# Entrypoint: populate Letta tool-exec venv on first start (survives the host
# volume mount that shadows anything baked into the image). The base Letta image
# already has an entrypoint at /usr/local/bin/docker-entrypoint.sh (the postgres
# init script), so we preserve it and chain to it from ours.
RUN mv /usr/local/bin/docker-entrypoint.sh /usr/local/bin/letta-docker-entrypoint.sh
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["./letta/server/startup.sh"]