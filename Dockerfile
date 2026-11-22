FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai

RUN apk add --no-cache tzdata \
    && addgroup -S -g 10001 monitor \
    && adduser -S -D -H -u 10001 -G monitor monitor \
    && mkdir -p /app /data/state /data/reports \
    && chown -R monitor:monitor /app /data

WORKDIR /app
COPY --chown=monitor:monitor *.py *.mjs /app/

USER 10001:10001

HEALTHCHECK --interval=60s --timeout=5s --start-period=180s --retries=3 \
  CMD ["python3", "-c", "import json,os,time; s=json.load(open('/data/state/monitor.json',encoding='utf-8')); p=json.load(open('/data/state/process.json',encoding='utf-8')); pid=int(p['pid']); os.kill(pid,0); age=time.time()-float(s.get('last_complete',0)); raise SystemExit(0 if 0 <= age < 600 else 1)"]

ENTRYPOINT ["python3", "-X", "utf8", "/app/shop_monitor.py"]
CMD ["--config", "/config/config.json", "--state", "/data/state/monitor.json", "--no-ai"]
