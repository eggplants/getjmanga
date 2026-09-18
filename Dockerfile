FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6 AS builder

ARG VERSION
ENV VERSION=${VERSION:-master}

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
RUN /opt/venv/bin/pip install --no-cache-dir \
    "git+https://github.com/eggplants/getjmanga@${VERSION}"

FROM al3xos/python-distroless:3.14-debian13
COPY --from=builder /opt/venv /opt/venv
# Nothing in here launches a venv, so the interpreter is called directly and told
# where the packages landed.
ENV PYTHONPATH="/opt/venv/lib/python3.14/site-packages"

ENTRYPOINT ["python", "/opt/venv/bin/getjmanga"]
