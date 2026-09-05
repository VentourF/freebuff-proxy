# register_service 容器化: freebuff 账号注册服务
# 依赖: OC 协议注册链路 (main.py/cf_mail.py) + CloakBrowser (反检测浏览器)
# CloakBrowser 二进制通过 volume 挂载宿主机 ~/.cloakbrowser 复用, 无需在镜像内下载
# 基于 python:3.13-slim (Debian bookworm) + apt 安装 Chromium 运行系统库
# (mcr.microsoft.com/playwright 镜像在国内网络拉不动, 改用 slim + 显式依赖)
FROM python:3.13-slim

USER root

# Chromium/CloakBrowser 运行所需系统库 (与 playwright deps 对齐, bookworm 包名)
# 分两层: 先 apt 后 pip, 便于缓存复用
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libx11-6 \
    libxcb1 \
    libxext6 \
    libxi6 \
    libxtst6 \
    fonts-liberation \
    fonts-noto-color-emoji \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY register_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# OC 协议链路 (main.py + cf_mail.py + web_config.json)
COPY oc/ /app/oc/

# 注册服务本体
COPY register_service/register_service.py /app/register_service/register_service.py
RUN mkdir -p /app/register_service/debug /app/credentials

ENV OC_DIR=/app/oc
ENV REG_HEADLESS=1
ENV PYTHONIOENCODING=utf-8

EXPOSE 8899

CMD ["python", "/app/register_service/register_service.py"]
