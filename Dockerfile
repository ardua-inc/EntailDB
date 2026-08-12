FROM python:3.13-slim

# pymssql needs FreeTDS; psycopg and pymysql ship binary wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        freetds-dev freetds-bin gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml ./
COPY src ./src
COPY app ./app

RUN pip install --no-cache-dir \
        fastapi "uvicorn[standard]" anthropic openai cryptography \
        "psycopg[binary]" PyMySQL pymssql \
    && pip install --no-cache-dir -e .

# Connection settings and the generated key live here. Mount it to persist.
ENV FIDELITY_DATA=/data
VOLUME /data
EXPOSE 8000

# Bound to 0.0.0.0 *inside the container only*; compose publishes it to
# 127.0.0.1 on the host. There is no authentication — see DESIGN.md.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
