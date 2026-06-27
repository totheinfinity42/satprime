FROM node:18-slim

WORKDIR /app

ENV NODE_OPTIONS=--openssl-legacy-provider

COPY package.json package-lock.json ./
RUN npm ci --production=false

COPY . .

RUN cd frontend && npm install --production=false && npm run build

ENTRYPOINT ["node", "server.js"]
