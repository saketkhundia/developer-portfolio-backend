FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Toolchains for the interactive playground runner (/exec/* in exec_service.py).
# Slim image: JDK (javac/java), C/C++ (gcc/g++), Node.js + TypeScript (tsc/node).
# Go, Rust, Ruby, C#, Kotlin stay on the batch sandbox (no live sessions).
RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jdk-headless gcc g++ nodejs npm \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g typescript

# Copy app files
COPY main.py .
COPY github.py .
COPY leetcode.py .
COPY analytics.py .
COPY exec_service.py .
COPY execution_engine.py .
COPY lang_config.py .

# Expose port (Cloud Run uses 8080)
EXPOSE 8080

# Run the app
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
