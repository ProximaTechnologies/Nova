FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        docker.io \
        iptables \
        passwd \
        python3 \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY dist/nova-supervisor-local-linux-amd64 /usr/local/bin/nova-supervisor
COPY local/dind/fake-nova.py /usr/local/bin/fake-nova.py
COPY local/dind/run-smoke.sh /usr/local/bin/run-smoke.sh

RUN chmod 0755 /usr/local/bin/nova-supervisor /usr/local/bin/fake-nova.py /usr/local/bin/run-smoke.sh

ENTRYPOINT ["/bin/bash", "/usr/local/bin/local-dind/run-smoke.sh"]
