FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --system --uid 10001 mcp
USER mcp
ENV MCP_TRANSPORT=streamable-http MCP_BIND_HOST=0.0.0.0 MCP_PORT=8765
EXPOSE 8765
CMD ["/app/.venv/bin/python", "-m", "listmonk_mcp.server"]
