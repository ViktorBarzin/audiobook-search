FROM python:3.13-slim-bookworm
WORKDIR /app
COPY requirements.txt .
# pip goes once the dependencies are in: nothing calls it at runtime, and its
# vendored urllib3, msgpack and setuptools carry High CVEs no pip release fixes yet.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip
COPY backend/ ./backend/
EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
