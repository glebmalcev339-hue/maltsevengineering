# Maltsev Engineering — магазин. Только stdlib, зависимостей нет.
FROM python:3.12-slim
WORKDIR /app
COPY . /app
# Постоянные данные (заказы + кеш карты) живут в /app/data:
# на хостинге подключите к этому пути постоянный диск, иначе заказы
# пропадут при первой же перевыкладке!
VOLUME /app/data
EXPOSE 8080
CMD ["sh", "-c", "python3 server.py --host 0.0.0.0 --port ${PORT:-8080}"]
