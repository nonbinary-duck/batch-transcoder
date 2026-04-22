FROM lscr.io/linuxserver/ffmpeg:latest

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# COPY requirements.txt /app/requirements.txt
# RUN python3 -m pip install --no-cache-dir -r /app/requirements.txt

COPY plan_transcodes.py /app/plan_transcodes.py
COPY run_transcodes.py /app/run_transcodes.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x \
    /app/plan_transcodes.py \
    /app/run_transcodes.py \
    /app/docker-entrypoint.sh

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["--help"]