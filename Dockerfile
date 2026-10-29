FROM python:3.12-slim
WORKDIR /app
ENV COVERLY_HOME=/app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-tam tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml requirements.txt ./
COPY coverly ./coverly
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 coverly && mkdir -p /app/archive && chown -R coverly:coverly /app
USER coverly
EXPOSE 8000
# Docker needs a container-visible bind; the app still validates localhost Host/Origin.
CMD ["python", "-m", "coverly.docker_entry"]
