FROM node:18-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libvips-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --production && npm cache clean --force

COPY *.js ./
COPY Images/ ./Images/
COPY Model/ ./Model/
COPY MobilenetModel/ ./MobilenetModel/

ENTRYPOINT ["node", "app.js"]
