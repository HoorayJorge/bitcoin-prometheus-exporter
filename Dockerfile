FROM docker.io/library/python:3.8-alpine3.15

LABEL org.opencontainers.image.title "garlicoin-prometheus-exporter"
LABEL org.opencontainers.image.description "Prometheus exporter for garlicoin nodes"

# Dependencies for python-bitcoinlib and sanity check.
RUN apk --no-cache add \
      binutils \
      libressl-dev \
      openssl-dev && \
    python -c "import ctypes, ctypes.util; ctypes.cdll.LoadLibrary('/usr/lib/libssl.so')"

RUN pip install --no-cache-dir \
        prometheus_client \
        python-bitcoinlib \
        riprova

RUN mkdir -p /monitor
ADD ./garlicoind-monitor.py /monitor

USER nobody

CMD ["/monitor/garlicoind-monitor.py"]
