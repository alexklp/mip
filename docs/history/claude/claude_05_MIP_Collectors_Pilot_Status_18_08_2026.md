# МІП — статус пілоту collectors/ingestion

**Дата:** 18.08.2026
**Етап:** Technical Pilot → collectors (веб/RSS запущено, Telegram — рішення ухвалене, реалізація в черзі)

## Що зроблено сьогодні

1. **Мінімальний DDL застосовано в `mip_dev`** (`~/mip/sql/001_ingestion_pilot.sql`), схема `public`, 4 таблиці — див. розділ "Структура БД" нижче. Свідомо пропущено на цьому етапі: `contour_id`/`contours`-довідник, партиціонування `raw_items`, native ENUM (замінено на `CHECK`), soft-dedup, embeddings. Це LLD pilot-scope рішення, не відхід від HLD.
2. **Обрано перше джерело:** RSS ТСН (`https://tsn.ua/rss/full.rss`) — з міркувань обсягу, для стрес-тесту dedup-логіки.
3. **Перевірено 7 живих UA-мовних RSS-фідів** (з ~100 кандидатів зі списку "ТОП-100 ЗМІ України", решта — 404/заблоковано фетчером/не UA): ТСН, Цензор.НЕТ, УНІАН, Zaxid.net, 24 канал, Еспресо, BBC News Україна.
4. **Написано і запущено перший RSS-collector** (`~/mip/collectors/rss_tsn.py`) на контракті: RSS entry → `raw_items` (raw, без дедуп) → normalization (strip HTML) → `content_items` (hard dedup за `content_hash`) → `item_occurrences` (ідемпотентно за `UNIQUE(source_id, external_ref)`). Без credentials — публічний RSS.
5. **PASS підтверджено на реальних даних, включно з ідемпотентністю на живій стрічці** (стрічка змінилась між прогонами, collector коректно відділив нове від уже баченого):
   - прогін 1: 184 raw / 184 new_content / 184 new_occurrence / 0 errors
   - прогін 2: 185 raw / 3 new_content / 3 new_occurrence / 182 dup_occurrence / 0 errors
   - **поточний стан БД: `raw_items`=369, `content_items`=187, `item_occurrences`=187**
6. **Рішення по Telegram:** обрано MTProto (Telethon) як цільове рішення для повної картини (коменти живуть у discussion-групі, недоступні без клієнтської сесії). Акаунт — **виділений, не особистий** (OPSEC). Реалізація відкладена (MVP-first), але для об'єму паралельно вирішено додати простий `t.me/s/` web-preview без логіна — див. оновлення 19.08.
7. **Отримано і зафіксовано в проєкті:**
   - `04_TZ_Monitoring_Requirements.md` — вихідне ТЗ, що визначає зміст 4 контурів моніторингу (Контур 1 — об'єкти ДШВ ЗСУ/OPSEC власних підрозділів, Контур 2 — світовий контекст, Контур 3 — загальнодержавний контекст, Контур 4 — ворожі медіа), раніше згадуваних у HLD (`contour_id`) без розкриття змісту.
   - список 72 ворожих Telegram-каналів (Контур 4) — стане джерелом даних для `sources` при підключенні Telegram-collector'а.
   - `03_MIP_Server_State_18.08.2026.md` — повний технічний handoff по серверу/LLM-стеку (Hermes+MamayLM), не по collectors.

## Структура БД (поточна, `mip_dev`, схема `public`)

```
sources
├─ source_id (PK, uuid)
├─ name, url_or_handle (text)
├─ source_type (text, CHECK: rss | telegram | web | api)
├─ contour_id (smallint, CHECK 1..4)          -- додано ввечері 18.08, груба класифікація за замовчуванням
├─ is_active (boolean)
└─ created_at (timestamptz)

raw_items                                    -- staging, без дедупу за задумом
├─ raw_item_id (PK, uuid)
├─ source_id (FK → sources)
├─ collected_at (timestamptz)
├─ content_hash (text)                       -- технічний, НЕ дедуп-ключ
├─ payload (jsonb)                           -- повний сирий обʼєкт (RSS entry / TG post)
└─ status (text, CHECK: pending | normalized | rejected)

content_items                                 -- канонічний контент
├─ content_id (PK, uuid)
├─ content_hash (text, UNIQUE)               -- дедуп-ключ, від нормалізованого тексту
├─ language, title, text_content
└─ first_seen_at (timestamptz)

item_occurrences                              -- факт появи контенту в джерелі
├─ occurrence_id (PK, uuid)
├─ content_id (FK → content_items)
├─ raw_item_id (uuid, без formal FK — traced, staging-шар)
├─ source_id (FK → sources)
├─ external_ref (text)                       -- канонічний URL / t.me/<channel>/<msg_id>
├─ published_at, collected_at (timestamptz)
└─ UNIQUE(source_id, external_ref)           -- ключ ідемпотентності повторного збору
```

Roles: `ovasyliev` — власник схеми/бази на dev-стенді (peer auth, без паролів у коді).

## Оновлення 18.08.2026 (вечір) — multi-source collector + RU-джерела через VPN

1. **`rss_worker.py`** — генералізований collector, замінив однодержавний `rss_tsn.py`. Читає всі активні `sources` з `source_type='rss'`, ізоляція помилок на двох рівнях (per-entry і per-source — один битий фід/запис не валить весь прогін). Виправлено баг: `feedparser` не піднімає `bozo=True` на HTTP-помилках (404 тощо), тільки на кривому XML — додано окрему перевірку `feed.get("status")`.
2. **`sql/002_add_contours_and_sources.sql` застосовано:** додано `contour_id` (smallint, CHECK 1..4) до `sources`, `UNIQUE(url_or_handle)`, зареєстровано 7 UA-джерел (усі — Контур 3, крім BBC News Україна — Контур 2).
3. **Додано 8 RU-джерел (Контур 4)** — навмисно ворожі медіа, мета не редакційна якість, а обсяг/різноманіття сирого контенту для майбутнього тюнінгу фільтрів/кластеризації: РИА Новости, ТАСС, Лента.ру, Газета.Ru, Комсомольская правда, Военное обозрение, ИноСМИ (+ Царьград — 404 на всіх спробах, deprioritized).
4. **Знайдено і продіагностовано мережеву блокировку `.ru`-доменів:** усі 7 джерел падали з ідентичною `SSL: CERTIFICATE_VERIFY_FAILED... self-signed certificate`. Підтверджено (не припущено) через `openssl s_client` — це **Fortinet FortiGuard SDNS Blocked Page**, тобто блок на рівні мережевого апплаянсу/DNS-фільтра, не проблема конкретного сайту. Свідомо НЕ обходили через `verify=False` (ризик підміни/MITM).
5. **Рішення: scoped VPN, не Tor** (явно відхилений користувачем), не повний тунель на весь сервер. Реалізовано:
   - ProtonVPN Free (WireGuard-конфіг, ручний, бо офіційний CLI-клієнт не працює headless — залежить від NetworkManager/gnome-keyring).
   - Окремий **network namespace** (`protonns`) + veth-пара (`10.200.200.1/30` хост ↔ `10.200.200.2/30` namespace) + NAT (`iptables MASQUERADE`, тільки для цієї підмережі) + окремий DNS (`/etc/netns/protonns/resolv.conf`) — щоб SSH/Postgres/apt на хості залишались поза тунелем.
   - `wg-quick` піднято **всередині** namespace (`ip netns exec protonns wg-quick up /etc/wireguard/pvpn1.conf`). Виправлено: прибрано рядок `DNS =` з конфігу (викликав `resolvconf: Access denied` — з namespace немає доступу до D-Bus/systemd-resolved хоста, DNS і так вирішено окремо).
   - `microsocks` (SOCKS5, легкий) піднято всередині namespace, слухає на veth-адресі `10.200.200.2:1080` — доступний з основного namespace **без sudo**, звичайним процесом.
   - Патч у `rss_worker.py`: `needs_proxy(url)` перевіряє hostname на `.ru`; для таких джерел фетч іде через `requests.get(..., proxies={"http"/"https": "socks5h://10.200.200.2:1080"})`, байти згодовуються в `feedparser.parse(resp.content)`. Решта джерел — як і раніше, напряму.
6. **Результат прогону після патчу:** 6 з 7 RU-джерел ожили і залили контент (РИА 99, ТАСС 100, Лента 198, Газета.Ru 10, КП 200, ИноСМИ 13 нових; `rejected` — короткий мусор типу "дивись також", очікувано). **Разом активних джерел: 14** (7 UA + 7 RU, з них 6 RU реально дають дані).
7. **Відомий відкритий issue:** `topwar.ru` (Военное обозрение) — `HTTP 403` вже НЕ від Fortinet, а від самого сайту (ймовірно ріже датацентрові/VPN-адреси ProtonVPN). Не мережева проблема, не критично, deprioritized за принципом "обсяг важливіший за ідеальне покриття кожного джерела".
8. **Технічний борг, не зроблено:** `microsocks` зараз висить фоновим job'ом поточної SSH-сесії (не переживе релогін/ребут). Namespace/veth/NAT-правила теж не персистентні (не пережиють ребут хоста). Потрібен systemd-юніт (namespace setup + `wg-quick` + `microsocks`) — окремий крок, ще не зроблено.

## Оновлення 19.08.2026 (ранок) — Telegram web-preview collector (без MTProto)

1. **Уточнено обсяг задачі:** сьогоднішній "додай телеграм" — це НЕ MTProto-конектор (той лишається відкладеним, потребує виділеного акаунта, ще не заведено). Йдеться про простий парсинг `t.me/s/<channel>` без логіна — публічний веб-превью каналу, для об'єму/різноманіття контенту, як і планувалось раніше.
2. **`source_type='web'`** — влізло в існуючий CHECK без DDL-змін.
3. **Прототип на одному каналі** (РИА Новости, `rian_ru`) — `collectors/tg_web_rian.py`, підтвердив контракт: пост → `raw_items` → normalize → `content_items` (dedup) → `item_occurrences` (idempotent, `external_ref = t.me/<channel>/<msg_id>`). PASS з першого разу: 20/20 new, ідемпотентність на повторному прогоні (20/20 dup). Дані в БД перевірено вручну (реальний текст новин, коректні `published_at`).
   - **Implementation detail:** у TG-поста немає окремого заголовка як в RSS — у `content_items.title` кладеться перші 200 символів тексту поста (псевдо-заголовок).
   - **Відоме сміття в тексті:** підпис каналу на кшталт "🔹 Подписаться на РИА Новости" тягнеться в контент — не баг, фільтрація такого — задача аналітичного шару пізніше, не collector'а.
4. **Генералізовано** в `collectors/tg_web_worker.py` (аналог `rss_worker.py`: цикл по активних `source_type='web'`, ізоляція помилок per-source, пауза 0.7с між каналами — ввічливість до `t.me`, не женемо скрейпінг).
5. **Зареєстровано всі 72 канали з "Основні_моніторингові_канали_противника.pdf"** (Контур 4) одним батчем — на відміну від RSS, тут немає ризику бану акаунта (анонімні HTTP GET, акаунта взагалі немає), тож batch-реєстрація одразу всіх виправдана.
6. **Результат першого повного прогону:** `errors=0` по всіх 72 джерелах (жоден не завалив прогін). **57 з 72 каналів реально віддають контент**, 15 — `0 posts`.
7. **Продіагностовано (не припущено) причину 0-postів:** перевірено на прикладі `rybar` — `curl` отримує `302` редирект на `t.me/rybar` (без `/s/`), `requests` (той самий шлях, що й у проді) в підсумку отримує `200`, валідний HTML (9670 байт), але це landing-сторінка Telegram "завантажте застосунок" (порожній `og:description`, `robots: noindex,nofollow`), а не стрічка постів. Це стандартна поведінка Telegram для каналу, що вимкнув анонімний веб-превью. **Перевірено для 1 з 15 каналів** (`rybar`) — для решти 14 не перевірено індивідуально, але симптом (та сама механіка Telegram) імовірно той самий. Це не баг collector'а, а відоме обмеження методу — саме тому MTProto лишається цільовим рішенням для повної картини.
   - Список 15 каналів без публічного превью: Рыбарь, Анатолий Шарий, Dambiev, REVERSE SIDE OF THE MEDAL, Вести, Андрей Медведев, Специально для RT, ГЕОПОЛИТИКА Z, Северный Ветер, Сыны Отечества, Військовий відділ РФ, Торпеда Z, Направленец по Украине, Канал вице-спикера ЛДПР, Егор Холмогоров.
8. **Стан БД після прогону:** `raw_total=4653`, `content_total=2126`, `occurrence_total=2126` (сукупно RSS + TG-web).
9. **Технічний борг (не зроблено):** аналогічно до RSS-worker'а — `tg_web_worker.py` поки що запускається вручну, не в persistent-режимі (systemd/cron ще не налаштовано, той самий пункт з учорашнього боргу).

## Наступні кроки (в порядку обговорення, не остаточний план)

1. ~~Переробити `rss_tsn.py` → multi-source collector~~ — **зроблено** (`rss_worker.py`).
2. ~~Batch-реєстрація RSS-джерел~~ — **зроблено для 14 джерел** (7 UA + 7 RU).
3. ~~Telegram web-preview collector~~ — **зроблено**, 57/72 каналів дають дані (`tg_web_worker.py`).
4. **Персистентність (перенесено з учора, ще актуально):** systemd-юніти для (а) namespace+VPN+microsocks обвʼязки, (б) `rss_worker.py` та `tg_web_worker.py` у нескінченному режимі.
5. (Дія користувача, не блокує решту) Виділений Telegram-акаунт + `api_id`/`api_hash` на my.telegram.org для MTProto-collector'а — досі відкладено, потрібен для (а) 15 каналів без веб-превью, (б) коментарів під постами.
6. Після наявності акаунта — окремий одноразовий інтерактивний login-скрипт (Telethon), session-файл поза git (`~/mip/.secrets/telegram/`, права 700/600).
