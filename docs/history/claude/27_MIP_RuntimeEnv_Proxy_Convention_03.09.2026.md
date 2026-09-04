# МІП — runtime env / RSS `.ru`-proxy / uv venv: рабочий baseline, не переисследовать

**Дата:** 03.09.2026 (доповнено 03.09.2026 — той самий день, друге напоминание)
**Статус:** операційна конвенція, зафіксована користувачем після того як я
двічі не здогадався про `$HOME/.config/mip/runtime.env` і намагався лагодити
неіснуючу проблему через ручний `export`, а потім ще раз наступив на сусідні
граблі — дав ad-hoc pilot-скрипт (`resolve_and_sectionize.py`, психопг) без
`uv run`, отримав `ModuleNotFoundError`. Не діагностувати заново без нового
симптому.

## Правило №1 (найчастіша помилка): будь-який python-скрипт у цьому репо — тільки через `uv run`

Не лише production ingest, **АБСОЛЮТНО будь-який** скрипт, що імпортує щось
із залежностей репо (`psycopg`, `trafilatura`, `lxml`, `requests`, ...) —
навіть одноразовий pilot/resolver без мережі й без proxy, як
`resolve_and_sectionize.py`. Залежності живуть у `uv`-venv, НЕ в системному
`python3` — голий `python3 script.py` впаде на першому ж `import psycopg` з
`ModuleNotFoundError`, це не баг скрипта.

```bash
cd ~/mip
uv run python3 path/to/script.py [args...]
```

Якщо `uv` не в PATH поточної сесії — `$HOME/.local/bin/uv run python3 ...`.

**Перед першим запуском будь-якого нового скрипта в цьому репо — типова
чек-лист-помилка: забути `uv run`. Перевіряти це першим, ще до аналізу
traceback.**

## Правило №2: runtime env (proxy) — де живе і як грузити

```
$HOME/.config/mip/runtime.env
```

Вне репозиторію `~/mip`, не коммітиться. Містить як мінімум
`MIP_RSS_PROXY_URL`. **Значення ніколи не друкувати** — ні в лог, ні в
stdout, ні в документацію, ні в git, ні в чат. Presence-check без
інтерполяції значення:

```bash
[ -n "$MIP_RSS_PROXY_URL" ] && echo SET || echo UNSET
```

Не `echo "$MIP_RSS_PROXY_URL"` і не `echo "${VAR:-default}"` — останнє
теж друкує реальне значення, якщо змінна встановлена (наступив на ці
граблі в цій же сесії).

Потрібен лише скриптам, що реально ходять у мережу (fetch article HTML і
подібне). Чисто read-only DB/parsing pilot (без HTTP) цього кроку не
потребує — але `uv run` (Правило №1) потрібен ЗАВЖДИ, незалежно від мережі.

```bash
cd ~/mip
set -a
source "$HOME/.config/mip/runtime.env"
set +a
uv run python3 ...
```

Порядок важливий: спочатку `source runtime.env` (якщо скрипт ходить у
мережу), потім `uv run python3`.

## Production ingest — уже обгорнутий

```
$HOME/mip/ops/run_ingest_once.sh
```

Сам робить `set -a; source runtime.env; set +a`, перевіряє що
`MIP_RSS_PROXY_URL` заданий, запускає RSS/Telegram workers через `uv`. Нові
worker'и, якщо потраплять у scheduled pipeline, повинні грузитись тим самим
wrapper-паттерном, не окремим механізмом.

## Proxy policy сама по собі — вже реалізована, не винаходити заново

`collectors/rss_worker.py` (і за зразком —
`experiments/occurrence_content/occurrence_content_worker.py`):

```python
hostname = urlparse(url).hostname or ""
if hostname.endswith(".ru"):
    proxy_url = os.environ["MIP_RSS_PROXY_URL"]  # падає з RuntimeError, якщо не задано
    requests.get(url, proxies={"http": proxy_url, "https": proxy_url}, ...)
else:
    requests.get(url, ...)  # direct
```

**Критичний нюанс, який легко забути:** сама наявність `MIP_RSS_PROXY_URL`
в environment НЕ змушує `requests` користуватись ним автоматично — код мусить
explicit передати `proxies=` для `.ru`-хостів. `source runtime.env` ≠
"мережа тепер через proxy" сама по собі.

## Що з цього робити новим worker'ам/pilot-скриптам

- Reuse точно ту саму логіку (`needs_proxy()`/`get_proxy_url()`), не заводити
  другий env var, не хардкодити proxy URL.
- Explicit fetch policy: `.ru` → proxy, решта → direct.
- Враховувати redirects: вхідний URL — `item_occurrences.external_ref`,
  фактичний — `response.url` (зберігати окремо, як `final_url`).
- Якщо мережевий шар через цю схему вже працює (proxy відповідає, `.ru`
  фетчиться) — вважати network layer робочим і не чіпати без нового симптому,
  зосереджуватись на extraction/business logic.
- **Будь-яку консольну команду для користувача в цьому репо давати одразу з
  `uv run python3` (і, якщо скрипт ходить у мережу, з `source runtime.env`
  перед ним) — не змушувати користувача самому дошукуватись ModuleNotFoundError.**
