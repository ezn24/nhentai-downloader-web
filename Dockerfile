# 使用最新 Python 映像
FROM python:3.12-slim

# 設定工作目錄
WORKDIR /app

# 安裝 git（以及常見基礎工具），完後清理 APT 快取
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 複製依賴檔案並安裝
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


# 下載官方 nhentai 專案並安裝成套件
RUN git clone https://github.com/RicterZ/nhentai
COPY patch.py ./nhentai
WORKDIR /app/nhentai

# 將 patch.py 的內容附加到 nhentai/util.py
RUN cat /app/nhentai/patch.py >> /app/nhentai/nhentai/util.py
RUN rm /app/nhentai/patch.py

RUN pip install --no-cache-dir .

# 回到主目錄
WORKDIR /app

# 複製你的檔案（包含 patch.py）
COPY . .



# 設定環境變數
ENV NHENTAI_PASSWORD=admin
ENV DOWNLOAD_PATH=/nhentai

# 對外開放 Flask 預設埠
EXPOSE 61234

# 啟動應用
CMD ["python", "nhentai.py"]
