# tau-search + presearch.
#
# Ships the tier-1 web tools (fetch.py, search.py) and the federated-search
# broker (presearch). Holds no index itself -- the registry lives in a JMFTS
# appliance this container talks to over HTTP.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRESEARCH_DATA=/data \
    JMFTS_API_URL=http://jmfts:8100

WORKDIR /app

# curl is the debugging tool you will actually want when a provider 403s.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py fetch.py search.py ./
COPY presearch/ ./presearch/

# Upstream corpora are large (Curlie unpacks to ~620 MB) and must survive a
# rebuild, so /data is a volume, never a layer.
RUN mkdir -p /data && useradd -u 10001 -m presearch && chown -R presearch /data /app
USER presearch
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "presearch.cli"]
CMD ["check"]
