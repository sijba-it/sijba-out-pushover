FROM docker.io/library/python:3.11-slim

WORKDIR /app
COPY main.py .

EXPOSE 9101
CMD ["python3", "main.py"]
