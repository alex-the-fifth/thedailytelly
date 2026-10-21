FROM python:3.12-slim
WORKDIR /app
COPY server/requirements-lock.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home telly && mkdir /data && chown telly:telly /data
COPY server/ ./server/
COPY scripts/ ./scripts/
USER telly
ENV DAILYTELLY_DATA=/data
EXPOSE 8000
CMD ["python","-m","uvicorn","app.main:app","--app-dir","server","--host","0.0.0.0","--port","8000","--workers","1","--no-access-log","--proxy-headers"]
