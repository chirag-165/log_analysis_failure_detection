FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY log_processor.py policy_engine.py ./
COPY failure_model_v2.pkl ./

CMD ["python", "log_processor.py"]
