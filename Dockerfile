# 本地化部署版：不使用远程拉取 worker.js（远程版会覆盖本地 flash 支持等改动），
# 直接运行构建时预置的 worker.js + server.js。
# NODE_USE_ENV_PROXY=1（fetch 走 HTTPS_PROXY 代理，free 模型要求美区出口）自 Node 22.9 起支持。
# 注: 使用本地缓存 node:22-alpine (v22.23.2) 以避免外部加速源拉取 node:24-alpine 失败(当前加速源均不可用)。
FROM node:22-alpine

WORKDIR /app

# 预置当前版本（含本地改动）
COPY package.json server.js worker.js admin.js ./
COPY static ./static

# 创建 credentials 目录（运行时挂载）
RUN mkdir -p /app/credentials

# 简单 entrypoint：直接启动，不拉远程
RUN printf '%s\n' \
    '#!/usr/bin/env sh' \
    '' \
    'exec node /app/server.js' \
    > /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER node
EXPOSE 8787

ENTRYPOINT ["/app/entrypoint.sh"]
