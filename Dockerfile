FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 国内网络可选加速：构建时传入 --build-arg USE_CN_MIRROR=1 即启用清华 pip 源与 npmmirror Playwright 源
ARG USE_CN_MIRROR=0
ENV TZ=Asia/Shanghai \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# 国内镜像：USE_CN_MIRROR=1 时同时把 apt 源切到清华镜像（否则国内直连 deb.debian.org 易失败导致构建中断）
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
        || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi

# 安装系统渲染依赖 + 中文字体（无字体会导致页面中文乱码/截图豆腐块）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    fonts-noto-cjk \
    libnss3 \
    libnspr4 \
    libasound2 \
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
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单并安装
COPY requirements.txt .
RUN if [ "$USE_CN_MIRROR" = "1" ]; then \
        PIP_MIRROR="-i https://pypi.tuna.tsinghua.edu.cn/simple"; \
        export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright; \
    fi; \
    pip install --no-cache-dir $PIP_MIRROR -r requirements.txt && \
    playwright install chromium

# 复制源码
COPY . .

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -sf -o /dev/null http://127.0.0.1:${PORT:-8000}/ || exit 1

CMD ["python", "app.py"]
