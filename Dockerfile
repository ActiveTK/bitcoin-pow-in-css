FROM python:3.12-slim
RUN pip install --no-cache-dir aiohttp==3.10.11
WORKDIR /app
COPY server/ /app/server/
COPY public/ /app/public/
ENV PUBLIC_DIR=/app/public DATA_DIR=/data PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python3", "server/app.py"]
