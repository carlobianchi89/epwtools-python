# Runs the EPW.tools MCP server over stdio (used by Glama's automated checks and any container-based MCP client).
# Read tools work without a key; set EPWTOOLS_API_KEY to use the generation tools.
FROM python:3.12-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[mcp]"
ENV PYTHONUNBUFFERED=1
CMD ["epw-tools-mcp"]
