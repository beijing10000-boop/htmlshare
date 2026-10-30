# 服务端镜像；同时把客户端打成 wheel 放进去，用户可以直接
#   pip install <分享站地址>/client/htmlshare-<版本>-py3-none-any.whl
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${PYTHON_IMAGE} AS client-wheel
ARG PIP_INDEX_URL=https://pypi.org/simple
WORKDIR /src
COPY client/pyproject.toml ./client/
COPY client/htmlshare ./client/htmlshare
RUN pip wheel --no-deps --retries 5 --timeout 120 -i "${PIP_INDEX_URL}" -w /wheels ./client

FROM ${PYTHON_IMAGE}
ARG PIP_INDEX_URL=https://pypi.org/simple
# TZ 用 POSIX 写法，slim 镜像里不一定有 tzdata
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=CST-8 \
    HTMLSHARE_DATA_DIR=/data \
    HTMLSHARE_CLIENT_DIST=/app/client-dist
WORKDIR /app
COPY server/requirements.txt .
RUN pip install --no-cache-dir --retries 5 --timeout 120 -i "${PIP_INDEX_URL}" -r requirements.txt
COPY server/htmlshare_server ./htmlshare_server
COPY --from=client-wheel /wheels ./client-dist
# 宿主机 umask 077 时 git pull 出来的文件是 0600，非 root 用户会读不到
RUN chmod -R a+rX /app \
    && useradd --system --uid 10001 --no-create-home app \
    && mkdir -p /data && chown app /data
USER app
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1
CMD ["uvicorn", "--factory", "htmlshare_server.app:create_app", "--host", "0.0.0.0", "--port", "8080"]
