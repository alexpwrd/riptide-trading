FROM nvidia/cuda:12.2.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# System essentials
RUN apt-get update && apt-get install -y \
    curl git python3 python3-pip python3-venv \
    ca-certificates nodejs npm wget jq bc \
    build-essential libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Kraken CLI
RUN curl --proto '=https' --tlsv1.2 -LsSf \
    https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh
ENV PATH="/root/.cargo/bin:${PATH}"

# Install Foundry (Solidity toolchain)
RUN curl -L https://foundry.paradigm.xyz | bash && \
    /root/.foundry/bin/foundryup
ENV PATH="/root/.foundry/bin:${PATH}"

# Python AI/ML stack
RUN pip3 install --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cu121
RUN pip3 install --no-cache-dir \
    transformers accelerate \
    anthropic openai \
    pandas numpy scipy scikit-learn \
    websockets aiohttp requests \
    python-dotenv pydantic \
    ta-lib-bin

WORKDIR /app

COPY requirements.txt* ./
RUN if [ -f requirements.txt ]; then pip3 install --no-cache-dir -r requirements.txt; fi

CMD ["bash"]
