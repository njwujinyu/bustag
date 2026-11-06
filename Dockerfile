# bustag 镜像。python:3.12-slim 自带 tzdata（TZ 才能生效），不需要再装系统包
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖再复制代码：只改代码时可以复用依赖层
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# MIT 许可要求每份副本都附带版权和许可声明
COPY LICENSE ./
COPY bustag ./bustag

LABEL org.opencontainers.image.source="https://github.com/njwujinyu/bustag" \
      org.opencontainers.image.description="bustag：给 JavBus 影片打标，训练模型并自动推荐（维护版）" \
      org.opencontainers.image.licenses="MIT"

# 数据库、配置文件、模型和封面缓存都在 /app/data
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as r; r.build_opener(r.ProxyHandler({})).open('http://127.0.0.1:' + (os.environ.get('BUSTAG_PORT') or '8000') + '/', timeout=4)"]

# 定时抓取由程序自己调度，Python 作为 PID 1 直接处理 SIGTERM（docker stop）
CMD ["python", "-m", "bustag.app.index"]
