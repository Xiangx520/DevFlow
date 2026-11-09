FROM node:24-bookworm-slim AS build
WORKDIR /opt/devflow/vendor/pi
COPY vendor/pi ./
RUN npm ci --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
COPY vendor/pi-model-data ./packages/ai/src/providers/data
RUN npm run build:offline
WORKDIR /opt/devflow/pi-service
COPY pi-service/package*.json ./
RUN npm install --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org
COPY pi-service/tsconfig.json ./
COPY pi-service/src ./src
RUN npm run build

FROM node:24-bookworm-slim
RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --no-install-recommends git python3 python3-pip python3-venv ripgrep ca-certificates && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/python && chown -R node:node /opt/python
ENV PATH="/opt/python/bin:$PATH"
COPY --from=build /opt/devflow /opt/devflow
RUN mkdir -p /workspace/repo /workspace/sessions && chown -R node:node /workspace
VOLUME ["/workspace"]
USER node
WORKDIR /workspace/repo
CMD ["node","/opt/devflow/pi-service/dist/main.js"]
