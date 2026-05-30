FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV NHENTAI_PASSWORD=admin
ENV DOWNLOAD_PATH=/nhentai
ENV DOUJINSHI_DL_URL=https://nhentai.net
ENV DOUJINSHI_DL_TOKEN=

EXPOSE 61234

CMD ["python", "nhentai.py"]
