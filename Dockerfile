FROM python:3.12-slim
LABEL org.opencontainers.image.source=https://github.com/groupsky/ev-journal

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 1000 evj \
    && mkdir /data && chown evj /data
USER evj
VOLUME /data
ENV EVJ_DATA_DIR=/data
ENTRYPOINT ["ev-journal"]
