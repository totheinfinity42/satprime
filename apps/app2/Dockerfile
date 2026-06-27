FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TF_CPP_MIN_LOG_LEVEL=2 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2

WORKDIR /edge

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libhdf5-103-1 \
        libjpeg62-turbo \
        libopenblas0 \
        libpng16-16 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-rpi.txt .
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements-rpi.txt

COPY src/app /edge/app
COPY docs/renato-boemer-satellite-remote-sensing.jpeg /edge/sample.jpeg

WORKDIR /edge/app

RUN python -c "from tensorflow.keras.models import load_model; model = load_model('best_model.h5', compile=False); print('Raspberry Pi inference model smoke test OK', len(model.outputs))"

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app \
    && useradd -u ${APP_UID} -g app -m appuser \
    && chown -R appuser:app /edge
USER appuser

EXPOSE 8000

CMD ["python", "predict.py", "--image", "/edge/sample.jpeg"]
