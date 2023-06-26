FROM nikolaik/python-nodejs:latest
RUN apt-get update && apt-get install -y libpq-dev gcc
RUN npm install -g nodemon
WORKDIR /opt/app
COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt
CMD nodemon -L --watch src -e py --exec "python3 src/main.py config.json"
