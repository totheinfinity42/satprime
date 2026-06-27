FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2 \
    MAX_JOBS=1 \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /usr/src/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        libglib2.0-0 \
        libgl1 \
        libgomp1 \
        libjpeg62-turbo \
        libopenblas0 \
        libpng16-16 \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-rpi.txt .

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --index-url ${TORCH_INDEX_URL} torch torchvision \
    && python -m pip install -r requirements-rpi.txt

COPY . .

RUN cd utils/nms_rotated \
    && python setup.py build_ext --inplace \
    && cd /usr/src/app \
    && python -c "import cv2, torch; from utils.nms_rotated import obb_nms; boxes=torch.tensor([[20.,20.,10.,10.,0.],[20.,20.,10.,10.,0.]]); scores=torch.tensor([0.9,0.8]); _, keep=obb_nms(boxes, scores, 0.5); assert keep.numel() == 1; print('Raspberry Pi CPU smoke test OK', torch.__version__, cv2.__version__)"

RUN mkdir -p /weights /usr/src/app/runs
COPY weights/best.pt /weights/best.pt

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app \
    && useradd -u ${APP_UID} -g app -m appuser \
    && mkdir -p /tmp/matplotlib \
    && chown -R appuser:app /usr/src/app /tmp/matplotlib
USER appuser

ENTRYPOINT ["python", "detect.py", "--device", "cpu"]
CMD ["--weights", "/weights/best.pt", "--source", "dataset/dataset_demo/images/P0032.png", "--imgsz", "640"]
