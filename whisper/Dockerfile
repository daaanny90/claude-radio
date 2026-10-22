# Optional CPU-only alternative to the native Mac whisper-server (compose profile whisper-docker).
# whisper.cpp is built on the machine that builds the image (GGML_NATIVE uses the instructions
# of the local CPU). The image is therefore not portable to another machine: rebuild it there.
FROM debian:bookworm-slim AS build
ARG WHISPER_VERSION=v1.9.5
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${WHISPER_VERSION} https://github.com/ggml-org/whisper.cpp /src
WORKDIR /src
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=ON -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON \
    && cmake --build build --config Release -j 2 --target whisper-server whisper-cli

FROM debian:bookworm-slim
# libgomp1: OpenMP, used by ggml for threads
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /src/build/bin/whisper-server /src/build/bin/whisper-cli /usr/local/bin/
WORKDIR /models
EXPOSE 8080
# The real command, with model and options, is in docker-compose.yml
CMD ["whisper-server", "--host", "0.0.0.0", "--port", "8080"]
