FROM python:3.14-alpine

WORKDIR /app

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY app/ .

CMD ["python", "main.py"]