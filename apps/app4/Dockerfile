ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG BUILD_TIMESTAMP
ARG TARGETPLATFORM=local
ARG VERSION=local

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DEFAULT_TIMEOUT=120
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        gdal-bin \
        git \
        libexpat1 \
        libgdal-dev \
        libgeos-dev \
        libglib2.0-0 \
        libgl1 \
        libproj-dev \
        libspatialindex-dev \
        pkg-config \
        proj-bin \
        proj-data \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/torchgeo

COPY pyproject.toml README.md LICENSE ./
COPY torchgeo ./torchgeo

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && python -c "import pathlib, torch, torchvision; pathlib.Path('/tmp/torch-cpu-constraints.txt').write_text(f'torch=={torch.__version__}\ntorchvision=={torchvision.__version__}\n')" \
    && python -m pip install --prefer-binary --constraint /tmp/torch-cpu-constraints.txt .

RUN python -c "import torch, torchvision, rasterio, geopandas, torchgeo; print('TorchGeo', torchgeo.__version__, 'ready on', '${TARGETPLATFORM}', 'with torch', torch.__version__, 'and torchvision', torchvision.__version__)"

ENTRYPOINT ["python", "-m", "torchgeo.rpi_entrypoint"]
