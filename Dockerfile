FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plugin.py .

# Exécution sans privilèges root
RUN useradd --system --create-home --home-dir /home/ipam ipam
USER ipam

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:9000/Plugin.Activate', method='POST'))" || exit 1

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:9000", "plugin:app"]
