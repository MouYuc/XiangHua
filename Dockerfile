# GuangPu-TTS 环境镜像（不含任何声音模型）
# 基于 GPT-SoVITS 官方 torch-base，构建时克隆引擎并打广普补丁
FROM node:24-alpine AS frontend-build
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM xxxxrt666/torch-base:cu12.6-full

LABEL maintainer="MouYuc"
LABEL description="GuangPu-TTS: GPT-SoVITS + Cantonese-accented Mandarin patch"

SHELL ["/bin/bash", "-c"]
WORKDIR /workspace/GPT-SoVITS

# 1. 克隆 GPT-SoVITS 引擎
# 锁定到补丁对应的 commit（d523079），避免上游更新导致 git apply 失败
RUN git clone https://github.com/RVC-Boss/GPT-SoVITS.git /tmp/gs \
    && cd /tmp/gs \
    && git checkout d523079fc05d9a8028d6085bffe4a2757c32abb6 \
    && cd /workspace/GPT-SoVITS \
    && cp -a /tmp/gs/. . \
    && rm -rf /tmp/gs

# 2. 应用广普发音词典补丁
COPY patches/pinyin.patch /tmp/pinyin.patch
RUN git apply /tmp/pinyin.patch \
    && rm /tmp/pinyin.patch

# 3. 构建工具 + ffmpeg（opencc/pyopenjtalk 等需要编译）
RUN apt-get update && apt-get install -y --no-install-recommends \
        cmake build-essential ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 4. 依赖（requirements.txt 由克隆的仓库提供）
RUN pip install -r extra-req.txt --no-deps \
    && pip install -r requirements.txt \
    && pip install faster-whisper noisereduce fastapi uvicorn python-multipart

# 5. 拷贝本仓库工具、训练脚本与 WebUI 启动器
COPY tools /workspace/tools
COPY backend /workspace/backend
COPY scripts /workspace/scripts
COPY patches /workspace/patches
COPY assets /workspace/assets
COPY tools/frontend_only_launcher.py /workspace/frontend_only_launcher.py
COPY --from=frontend-build /frontend/dist /workspace/frontend/dist

ENV PYTHONPATH="/workspace:/workspace/GPT-SoVITS"
ENV GPT_SOVITS_ROOT="/workspace/GPT-SoVITS"
ENV XIANGHUA_SERVER_NAME="0.0.0.0"
ENV XIANGHUA_SERVER_PORT="7860"

WORKDIR /workspace
EXPOSE 7860 9874

CMD ["python", "/workspace/frontend_only_launcher.py"]
