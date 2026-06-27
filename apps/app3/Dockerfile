FROM python:3.10-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /pyraws

RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
        libexpat1 \
        libgdal32 \
        libgeos-c1v5 \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        libjpeg62-turbo \
        libopenblas0 \
        libpng16-16 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-rpi.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements-rpi.txt

COPY pyraws /pyraws/pyraws
COPY data /pyraws/data
COPY smoke_test.py /pyraws/smoke_test.py

RUN python -c "from pathlib import Path; root = Path('/pyraws'); (root / 'pyraws' / 'sys_cfg.py').write_text('PYRAWS_HOME_PATH = \"{}\"\\nDATA_PATH = \"{}\"\\n'.format(root, root / 'data'), encoding='utf-8')"

RUN python -m py_compile smoke_test.py \
    && mkdir -p /tmp/matplotlib

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app \
    && useradd -u ${APP_UID} -g app -m appuser \
    && chown -R appuser:app /pyraws /tmp/matplotlib
USER appuser

CMD ["python", "smoke_test.py"]
