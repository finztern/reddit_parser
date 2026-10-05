FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY scraper/ ./scraper/

# config.yaml, accounts.yaml и cookies/ монтируются как volume в docker-compose.yml,
# чтобы их можно было менять (в т.ч. включать новые аккаунты, крутить target_rate)
# без пересборки образа.
# (top_subreddits.csv больше не копируется: раскладка по сабам ушла вместе
# с scraper/grouping.py, опрашивается один поток stream_subs.)

CMD ["python3", "main.py"]
