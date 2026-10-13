FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BIND_HOST=0.0.0.0 TOKEN_FILE=/app/data/tokens.json OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
RUN groupadd --gid 10001 quant && useradd --uid 10001 --gid quant --no-create-home quant
COPY requirements.txt .
COPY vendor ./vendor
RUN pip install --no-cache-dir --upgrade pip==26.2.1 && pip install --no-cache-dir -r requirements.txt && pip check
COPY --chown=quant:quant live_quant_engine.py dashboard_auth.py .
COPY --chown=quant:quant web ./web
COPY --chown=quant:quant Generating_Keys/get_token.py ./Generating_Keys/get_token.py
RUN mkdir /app/data && chown quant:quant /app/data
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3)"
CMD ["python", "live_quant_engine.py"]

COPY --chown=quant:quant scripts/dashboard-password.py ./scripts/dashboard-password.py
