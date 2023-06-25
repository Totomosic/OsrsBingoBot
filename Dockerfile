FROM python:3.10-slim-buster
RUN apt-get update && apt-get install -y libpq-dev gcc
WORKDIR /opt/app
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt
CMD python3 src/main.py config.json
