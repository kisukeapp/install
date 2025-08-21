FROM ubuntu:22.04

ENV USERNAME=devuser \
    PASSWORD=devpass

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        openssh-server \
        openssh-client \
        sudo \
        apt-utils \
        tar \
        bash \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
COPY . .
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

RUN bash /tmp/setup.sh --install && \
    rm -rf /root/.kisuke

RUN mkdir /var/run/sshd
EXPOSE 22

ENTRYPOINT ["/entrypoint.sh"]

CMD ["/usr/sbin/sshd", "-D"]
