# syntax=docker/dockerfile:1
FROM python:3.12-alpine

ARG VERSION=dev
ARG SOURCE_URL=""
LABEL org.opencontainers.image.title="lorawan-ttn-metrics" \
      org.opencontainers.image.description="Prometheus exporter for a The Things Network gateway (Events API v3)" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.licenses="NOASSERTION"

# tzdata is required by zoneinfo for the --timezone option.
RUN apk add --no-cache tzdata \
 && adduser -D -H -u 10001 exporter

WORKDIR /app
COPY ttn_live.py /app/ttn_live.py

ENV PYTHONUNBUFFERED=1 \
    TTN_GATEWAY_ID=backboard2062 \
    TTN_SERVER=https://eu1.cloud.thethings.network \
    TTN_TIMEZONE=Europe/Paris
# TTN_API_KEY must be supplied at run time; it is never baked into the image.

USER 10001
EXPOSE 9877

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9877/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["python", "/app/ttn_live.py"]
CMD ["--no-events", "--summary", "0", "--metrics-port", "9877", "--metrics-host", "0.0.0.0"]
