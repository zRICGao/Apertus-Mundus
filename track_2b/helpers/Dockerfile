# CPU wheels make the default stack work without NVIDIA drivers.
FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/opt/models HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch==2.8.0 --index-url ${TORCH_INDEX_URL}
COPY helpers/requirements.txt /opt/helpers/requirements.txt
RUN pip install --no-cache-dir -r /opt/helpers/requirements.txt
ARG KEV_COMMIT=1f04cfa28e6a708485b28a9585010b9e436b776e
# The server uses the inference dependencies above, without the upstream training stack.
RUN pip install --no-cache-dir --no-deps "kev @ git+https://github.com/jaredpalmer/kev.git@${KEV_COMMIT}"
COPY helpers/ /opt/helpers/
WORKDIR /opt/helpers

FROM base AS embedding
RUN python download_models.py embedding
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HELPER_ROLE=embedding
CMD ["python", "serve.py"]

FROM base AS reranker
RUN python download_models.py reranker
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HELPER_ROLE=reranker
CMD ["python", "serve.py"]

FROM base AS kev
RUN python download_models.py kev
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HELPER_ROLE=kev KEV_BACKEND=torch KEV_FUSED=0 KEV_CUDA_GRAPHS=0 KEV_PREFIX_CACHE_SIZE=0
CMD ["python", "serve.py"]
