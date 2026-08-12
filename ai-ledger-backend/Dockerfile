FROM python:3.10-slim

# 安装编译 psycopg2 所需的轻量级 Linux 依赖工具
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# 创建一个非 root 用户，这是 Hugging Face 安全机制的硬性要求
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# 将当前目录的代码全部复制进容器
COPY --chown=user . $HOME/app

# 安装依赖
RUN pip install --no-cache-dir --upgrade -r requirements.txt

# 启动 FastAPI，抱脸社区默认必须监听 7860 端口
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]