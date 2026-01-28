FROM python:3.13-slim

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

ENV PORT=8000
EXPOSE 8000

# If your entrypoint is app.py (adjust as needed)
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000"]
