FROM python:3.13-slim

# System dependencies for psycopg2 and build tools
RUN apt-get update && apt-get install -y libpq-dev gcc && rm -rf /var/lib/apt/lists/*

# Non-root user required by standard container environments (e.g. Hugging Face Spaces)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

# Install Python dependencies
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user app ./app
COPY --chown=user migrations/__init__.py migrations/runner.py ./migrations/
COPY --chown=user migrations/simplified ./migrations/simplified

# Boot the target FastAPI application on port 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
