FROM python:3.9-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV MAX_JOBS=4
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsm6 libxext6 git ninja-build \
    libglib2.0-0 libxrender-dev build-essential \
    gdal-bin libgdal-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

ENV GDAL_CONFIG=/usr/bin/gdal-config

RUN pip install --upgrade pip \
    && echo "numpy<2.0" > /tmp/constraints.txt \
    && pip install cython "numpy<2.0"

RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

RUN pip install "mmcv-full>=1.5.3,<=1.8.0" "mmdet>=2.25.1,<3.0.0"

COPY requirements/ /mmrotate/requirements/
WORKDIR /mmrotate

RUN pip install rasterio --no-build-isolation
RUN pip install -r requirements/custom.txt

COPY . /mmrotate
RUN pip install -e .

ENTRYPOINT ["python", "demo/image_demo.py"]
CMD ["demo/demo.jpg", "oriented_rcnn_r50_fpn_1x_dota_le90.py", "oriented_rcnn_r50_fpn_1x_dota_le90-6d2b2ce0.pth", "--out-file", "result.jpg", "--device", "cpu"]
