FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libglib2.0-0 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./

RUN python -c "from pathlib import Path; p=Path('requirements.txt'); b=p.read_bytes(); t=b.decode('utf-16') if b[:2] in (b'\xff\xfe', b'\xfe\xff') else b.decode('utf-8'); p.write_text(t, encoding='utf-8')"

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

CMD ["python", "webapp.py"]
