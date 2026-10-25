# A generic RunPod Serverless worker with a CUDA 13 C++ toolchain (see README.md).
# The base image is pinned by digest: nvidia/cuda:13.4.3-devel-ubuntu24.04 (linux/amd64).
FROM nvidia/cuda:13.4.3-devel-ubuntu24.04@sha256:cd530dd9b42a4866c2480b21cad7db0ff7b9c5b48c36455e7284328922963367

# NVIDIA_DISABLE_REQUIRE: the image asks for a CUDA 13.4 driver; a CUDA 13.0 (r580) driver runs it through
# minor-version compatibility as long as the code carries SASS for the GPU (no PTX JIT of newer PTX).
ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_DISABLE_REQUIRE=1 \
    PYTHONUNBUFFERED=1 \
    WORK_ROOT=/tmp

RUN apt-get update \
 && apt-get install -y --no-install-recommends cmake ninja-build g++-14 python3 python3-venv git ca-certificates curl xz-utils \
 && rm -rf /var/lib/apt/lists/*

# The worker's own Python lives in a venv off PATH; jobs see the system python3.
RUN python3 -m venv /opt/worker \
 && /opt/worker/bin/pip install --no-cache-dir runpod==1.12.0

COPY worker.py handler.py /opt/worker/
CMD ["/opt/worker/bin/python", "-u", "/opt/worker/handler.py"]
