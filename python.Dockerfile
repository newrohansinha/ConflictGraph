FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY python/ python/
RUN pip install --no-cache-dir ".[api,ml]"
COPY conflictgraph.example.yaml ./
RUN useradd --create-home --uid 10001 conflictgraph && mkdir -p /app/artifacts && chown -R conflictgraph:conflictgraph /app
USER conflictgraph
EXPOSE 8090
ENTRYPOINT ["conflictgraph"]

