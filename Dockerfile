# 基于 GPT-SoVITS 官方 torch-base，构建时克隆引擎并注入 V11 token 回调
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

# 3. 构建工具 + ffmpeg（opencc/pyopenjtalk 等需要编译）
# 构建网络访问 Ubuntu 官方源不稳定时，切换到国内镜像源
RUN find /etc/apt -type f \( -name '*.list' -o -name '*.sources' \) -exec sed -i \
        -e 's#http://archive.ubuntu.com#https://mirrors.tuna.tsinghua.edu.cn#g' \
        -e 's#https://archive.ubuntu.com#https://mirrors.tuna.tsinghua.edu.cn#g' \
        -e 's#http://security.ubuntu.com#https://mirrors.tuna.tsinghua.edu.cn#g' \
        -e 's#https://security.ubuntu.com#https://mirrors.tuna.tsinghua.edu.cn#g' \
        {} + \
    && apt-get update && apt-get install -y --no-install-recommends \
        cmake build-essential ffmpeg mecab libmecab-dev mecab-ipadic-utf8 \
        && rm -rf /var/lib/apt/lists/*

# 4. 依赖（requirements.txt 由克隆的仓库提供）
RUN pip install -r extra-req.txt --no-deps \
    && pip install -r requirements.txt \
    && pip cache purge

# eunjeon 需要 Ubuntu 系统 g++；Conda GCC 与 Jammy 头文件存在兼容性问题
RUN PATH="/usr/bin:/bin:/root/conda/bin" /root/conda/bin/pip install \
    faster-whisper noisereduce eunjeon fastapi uvicorn python-multipart

# 5. 注入 V11 语义 token 回调；发音补丁由用户运行时导入
COPY tools/gpt_sovits_orb_patch.py /tmp/gpt_sovits_orb_patch.py
RUN python /tmp/gpt_sovits_orb_patch.py /workspace/GPT-SoVITS \
    && grep -q semantic_token_callback GPT_SoVITS/AR/models/t2s_model.py \
    && grep -q semantic_token_callback GPT_SoVITS/TTS_infer_pack/TTS.py \
    && rm /tmp/gpt_sovits_orb_patch.py

COPY tools/gpt_sovits_mixed_language_patch.py /tmp/gpt_sovits_mixed_language_patch.py
RUN python /tmp/gpt_sovits_mixed_language_patch.py /workspace/GPT-SoVITS \
    && grep -q preprocess_segments GPT_SoVITS/TTS_infer_pack/TextPreprocessor.py \
    && grep -q segment_mixed GPT_SoVITS/TTS_infer_pack/TTS.py \
    && rm /tmp/gpt_sovits_mixed_language_patch.py

# 6. 拷贝本仓库工具、训练脚本与 WebUI 启动器
COPY tools /workspace/tools
COPY backend /workspace/backend
COPY scripts /workspace/scripts
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
