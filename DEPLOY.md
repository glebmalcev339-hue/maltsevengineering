# Maltsev Engineering — запуск магазина с приёмом заказов и оплаты

Папка — полный магазин: витрина + сервер + оплата. Нужны только VPS, домен
и ключи ЮKassa. Всё на стандартной библиотеке Python — `pip install` не нужен.

## Вариант А: проверка на своём компьютере (5 минут, без денег)

1. Установите Python 3.10+ (Windows: https://www.python.org/downloads/,
   при установке — галочка «Add python.exe to PATH»).
2. Распакуйте архив, запустите **`START_WINDOWS.bat`** (macOS/Linux:
   `python3 server.py --open`). Откроется магазин.
3. Оформите тестовый заказ с онлайн-оплатой — платёж учебный (mock),
   денег никуда не уходит.
4. Админка: **`OPEN_ADMIN.bat`** (пароль — в открывшемся `ADMIN_ACCESS.txt`).

## Вариант Б: боевой сайт (VPS + домен + ЮKassa)

### 1. VPS

Любой VPS от ~200 ₽/мес: Ubuntu 22.04/24.04, 1 ГБ RAM достаточно
(Timeweb, Beget, AEZA, Selectel — подойдёт любой). Нужен root/SSH-доступ.

```bash
# на сервере под root
apt update && apt install -y python3 nginx certbot python3-certbot-nginx
useradd -m shop
mkdir -p /home/shop/maltsev && chown shop:shop /home/shop/maltsev
```

### 2. Загрузка файлов

Скопируйте **содержимое** этой папки в `/home/shop/maltsev/` (WinSCP/FileZilla
или `scp`). Папку `data/` и `ADMIN_ACCESS.txt` со своего компьютера
**не копируйте** — на сервере создадутся свои.

```bash
chown -R shop:shop /home/shop/maltsev
```

### 3. Домен и HTTPS

1. Купите домен (например, maltsev-engineering.ru) и направьте A-запись
   на IP сервера.
2. Поставьте `nginx-maltsev.conf` из архива в `/etc/nginx/sites-enabled/maltsev`,
   заменив `example.com` на свой домен:
   ```bash
   nano /etc/nginx/sites-enabled/maltsev   # заменить example.com
   nginx -t && systemctl reload nginx
   ```
3. Выпустите сертификат:
   ```bash
   certbot --nginx -d ваш-домен -d www.ваш-домен --redirect -m ваша-почта --agree-tos -n
   ```

Nginx отдаёт статику сам, а `/api/*`, `/pay/*` и HTML-страницы проксирует
на Python (см. конфиг).

### 4. Автозапуск сервера

```bash
cp /home/shop/maltsev/maltsev.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now maltsev
journalctl -u maltsev -f   # контроль: должны показаться адреса магазина
```

Пароль админки создастся при первом старте:

```bash
cat /home/shop/maltsev/ADMIN_ACCESS.txt   # никому не показывайте
```

### 5. ЮKassa (боевые деньги)

1. Зарегистрируйте магазин на yookassa.ru (ИП/ООО), возьмите **shopId**
   и секретный ключ. Сначала — тестовый ключ!
2. Создайте `/home/shop/maltsev/config.json` (права только владельцу):
   ```json
   {
     "yookassa_shop_id": "123456",
     "yookassa_secret": "test_....",
     "public_base_url": "https://ваш-домен"
   }
   ```
   ```bash
   chmod 600 /home/shop/maltsev/config.json
   systemctl restart maltsev
   ```
3. В личном кабинете ЮKassa включите вебхуки на
   `https://ваш-домен/api/payments/notification`
   (события `payment.succeeded`, `payment.canceled`).
4. Проверьте оплату тестовой картой, затем замените ключ на боевой
   (`live_...`) и перезапустите сервис.

Проверить боевую оплату можно даже локально, до покупки VPS:
в `config.json` укажите `"public_base_url": "http://localhost:8080"`,
перезапустите `START_WINDOWS.bat` и оплатите тестовой картой —
браузер вернётся на ваш компьютер, а сервер сам опросит ЮKassa
и подтвердит платёж (вебхуки до localhost не доходят, опрос их заменяет).
В production с HTTPS работают и вебхуки, и опрос.

Пока ключей нет — сервер работает в **mock-режиме**: заказы настоящие
(пишутся в базу), а «оплата» учебная. В логе при старте всегда видно режим.

### 6. После запуска домена — обновить SEO-адреса

В файлах захардкожен `https://maltsev-engineering.ru`. Если ваш домен другой,
замените его ВЕЗДЕ (market.html, admin.html, robots.txt, sitemap.xml,
market.yml, manifest.webmanifest):

```bash
cd /home/shop/maltsev
grep -rl 'maltsev-engineering.ru' . --exclude-dir=data | xargs sed -i 's/maltsev-engineering.ru/ваш-домен/g'
systemctl restart maltsev
```

Без этого поисковики увидят чужой canonical!

## Резервные копии

- Раз в день копируйте `data/orders.sqlite3` (остановите сервис на минуту
  или копируйте файл — SQLite переживёт) и храните `ADMIN_ACCESS.txt` в
  приватном месте. Быстрый бэкап — кнопка экспорта JSON в админке.
- Простой cron:
  ```bash
  0 4 * * * tar -czf /root/shop-backup-$(date +\%F).tgz -C /home/shop maltsev/data
  ```

## Обновление магазина

1. Остановите сервис: `systemctl stop maltsev`.
2. Скопируйте новые файлы поверх, **не трогая `data/` и `ADMIN_ACCESS.txt`**.
3. Запустите: `systemctl start maltsev`. Заказы и пароль сохранятся.

## Безопасность

- `ADMIN_ACCESS.txt`, `config.json`, `data/` имеют права только владельцу;
  сервер по HTTP отдаёт только явный список публичных файлов.
- Админка — по Bearer-токену на 8 часов, пароль — PBKDF2-SHA256.
- Не открывайте порт 8080 наружу в firewall: сайт должен идти через Nginx+HTTPS.
- JSON заказов содержит персональные данные — не публикуйте экспорты.

## Если что-то не работает

```bash
journalctl -u maltsev -n 50        # логи сервера
systemctl status maltsev nginx     # статусы служб
curl -s http://127.0.0.1:8080/api/admin/session -H "Authorization: Bearer x"
# должен ответить {"error": ...} — значит, Python жив
```

Частые причины: не заменён домен в nginx-конфиге, нет A-записи,
забыт `systemctl restart maltsev` после config.json.

Поиск ПВЗ работает на открытых данных OpenStreetMap через движок Photon:
обычно это 2–7 секунд на город. В поставку уже вшит прогретый кеш
топ-40 городов (`data/cache.sqlite3`, действует 30 дней) — для них поиск
мгновенный. Запасные уровни: Overpass API, затем последний известный
результат из кеша. Если всё недоступно — покупатель вводит проверенный
адрес ПВЗ вручную (это всегда работает).

Обновить/прогреть кеш (например, раз в месяц или для своих городов):

```bash
python3 prewarm_cache.py
```

Список городов — в начале файла `prewarm_cache.py`, добавьте свои при нужде.

### Официальная карта Ozon (второй этап)

У Ozon есть официальный API для магазинов (Ozon Logistics / Seller API):
методы карты точек, списка ПВЗ и карточек точек — это «та самая» карта
с реальными пунктами, сроками и рейтингами. Для неё нужны ключи продавца:

1. Зарегистрируйтесь как продавец на seller.ozon.ru (нужны ИП/ООО).
2. В кабинете: Настройки → Управление частными приложениями → Создать,
   уровень доступа `seller-api.ozon-logistics`.
3. Пришлите мне Client ID и API-ключ — я подключу официальные данные
   (ключи потом можно отозвать и перевыпустить).

Пока ключей нет — работает быстрая карта на открытых данных выше.

## 6. Выкладка в интернет (хостинг)

Самый простой путь для этого магазина — российская PaaS-платформа
Amvera: деплой из Git по Dockerfile, бесплатный HTTPS, тарифы от ~170 ₽/мес.
Альтернатива «когда вырастете» — VPS (Timeweb/Selectel) + свой nginx,
там всё то же самое, но сервер настраиваете вы.

### 6.1. Подготовка кода (один раз)

1. Зарегистрируйтесь на github.com, создайте приватный репозиторий,
   например `maltsev-shop`.
2. Залейте туда **содержимое** папки `maltsev-shop` (кнопка
   «uploading an existing file» — можно прямо через браузер):
   `server.py`, `market.html`, `Dockerfile`, `cache_seed.sqlite3` и остальные.
   НЕ заливайте: `config.json` (ключи!), `ADMIN_ACCESS.txt`,
   `data/orders.sqlite3*` (ваши заказы).

### 6.2. Проект на Amvera

1. Регистрация на amvera.ru → «Создать проект» → «Приложение».
2. Подключите GitHub-репозиторий, сборка — по Dockerfile из корня.
3. Вкладка «Диски»: добавьте постоянный диск и примонтируйте его
   к пути `/app/data` — там живут заказы и кеш карты.
   Без диска заказы пропадут при первой перевыкладке!
4. Вкладка «Переменные», добавьте:
   - `ADMIN_PASSWORD` — ваш пароль админки (придумайте длинный);
   - `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET` — ключи ЮKassa;
   - `PUBLIC_BASE_URL` — адрес проекта, например
     `https://maltsev-shop.amvera.io` (дадут после запуска;
     сначала запустите без него, потом допишите и перезапустите).
5. Запустите проект. Проверка: откройте выданный адрес + `/market.html`,
   оформите тестовый заказ, войдите в `/admin.html` с `ADMIN_PASSWORD`.

### 6.3. Свой домен

1. Купите домен `.ru` (beget.ru/reg.ru/timeweb.ru, несколько сотен ₽/год).
2. В панели Amvera: проект → «Домены» → добавить → пропишите
   у регистратора указанную DNS-запись → HTTPS выпустится сам.
3. Обновите `PUBLIC_BASE_URL` на `https://ваш-домен` и перезапустите.
4. В кабинете ЮKassa укажите webhook:
   `https://ваш-домен/api/payments/notification`.

### 6.4. Обновления сайта

Залили новые файлы в GitHub → в Amvera нажали «Перевыложить» →
заказы на месте (они на диске), код новый. Перед обновлением —
«Экспорт» заказов из админки, на всякий случай.
