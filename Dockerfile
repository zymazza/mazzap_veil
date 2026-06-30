# syntax=docker/dockerfile:1
# VEIL — the Node static server and the full Python geospatial pipeline in one
# image. The hard part of the pipeline is GDAL with its version-matched Python
# (osgeo) bindings, numpy, and GDAL's array support (ReadAsArray / WriteArray);
# the official OSGeo GDAL "ubuntu-full" image ships all of that, so we base on it
# and add Node plus the pure-Python deps (pyproj, Pillow, mcp). The Meshtastic
# live bridge runs from a separate venv so its radio/client deps do not disturb
# the geospatial Python stack. PDAL remains optional; if the command is absent,
# the AOI builder skips LiDAR-derived DSM/DTM generation.
#
# Pin GDAL via GDAL_VERSION (this pins Python, numpy, and the bindings together);
# pin the pip deps in requirements.txt; pin Node via NODE_MAJOR.
ARG GDAL_VERSION=3.9.3
FROM ghcr.io/osgeo/gdal:ubuntu-full-${GDAL_VERSION}

ARG NODE_MAJOR=20

# Node (from NodeSource) + pip. python3, osgeo, and numpy already come from the
# base image; we only need pip to add the pure-Python deps below.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bluetooth bluez ca-certificates curl dbus gnupg \
      python3-pip python3-venv udev usbutils \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Pure-Python deps only. numpy and GDAL/osgeo come from the base image, pinned by
# GDAL_VERSION. The base image's python is the only interpreter here, so install
# straight into it (PEP 668 / externally-managed).
COPY requirements.txt requirements-live.txt ./
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt \
 && python3 -m venv /opt/veil-live \
 && /opt/veil-live/bin/python -m pip install --no-cache-dir --upgrade pip \
 && /opt/veil-live/bin/pip install --no-cache-dir -r requirements-live.txt

# Engine code, the bundled us-national pack, and the committed demo AOI. Twin
# data is private and gitignored; mount it at runtime (see docker-compose.yml).
COPY . .

# server.js binds HOST:PORT — bind all interfaces so the port is reachable from
# the host. TWIN_DATA_DIR points at the mounted twin (./data by default).
ENV HOST=0.0.0.0 \
    PORT=4173 \
    TWIN_DATA_DIR=/app/data \
    VEIL_LIVE_PYTHON=/opt/veil-live/bin/python
EXPOSE 4173

# Default: serve the viewer. Override to run a pipeline step, e.g.
#   docker compose run --rm veil npm run demo
CMD ["node", "server.js"]
