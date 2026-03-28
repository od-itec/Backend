FROM python:3.12 AS docker
RUN <<EOF
apt remove \
    $(dpkg --get-selections docker.io docker-compose \
            docker-doc podman-docker containerd runc \
    | cut -f1)
EOF

RUN <<EOF
apt update
apt install ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
EOF

COPY --link compose.d/docker.sources /etc/apt/sources.list.d/docker.sources
RUN --mount=type=cache,sharing=private,target=/var/cache/apt     \
    --mount=type=cache,sharing=private,target=/var/lib/apt/lists \
    apt update && apt install -y docker-ce docker-ce-cli         \
        containerd.io docker-buildx-plugin docker-compose-plugin

FROM docker AS base
WORKDIR /app
RUN --mount=source=/requirements.txt,target=/app/requirements.txt \
    --mount=type=cache,sharing=private,target=/root/.cache        \
  pip install -r requirements.txt

FROM base AS runtime
COPY --link .env .env
COPY --link src/ .
ENTRYPOINT [ "python", "main.py" ]
