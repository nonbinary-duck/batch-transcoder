FROM lscr.io/linuxserver/ffmpeg:latest

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/ /app/scripts/

RUN chmod +x \
    /app/scripts/plan_transcodes.py \
    /app/scripts/run_transcodes.py \
    /app/scripts/docker-entrypoint.sh

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["--help"]