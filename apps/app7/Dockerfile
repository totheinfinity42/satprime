FROM node:18-slim

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --production

COPY classify_new.mjs ./
COPY wrapper.js ./
COPY model/ ./model/
COPY rabbit.jpg ./

CMD ["node", "classify_new.mjs", "rabbit.jpg"]
