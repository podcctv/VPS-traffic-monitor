FROM python:3.12-slim
WORKDIR /app
COPY central /app/central
COPY scripts /app/scripts
COPY agent /app/agent
RUN pip install --no-cache-dir fastapi uvicorn
EXPOSE 8000
CMD ["uvicorn", "central.server:app", "--host", "0.0.0.0", "--port", "8000"]
