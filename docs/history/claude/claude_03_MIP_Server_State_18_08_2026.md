# МІП — технічний звіт про фактично виконані роботи та поточний стан

**Стан на:** 18.08.2026
**Етап:** Technical Pilot → перехід до collectors / ingestion реальних джерел

> Це фактологічний handoff-звіт (без архітектурних міркувань — ті в HLD/roadmap). Призначення: щоб будь-яка сесія/асистент, що підключається до проєкту пізніше, не перепитувала і не переналаштовувала вже підтверджене.

## 1. Контекст

Робочий цикл: Data Model → DDL → LLD/data contracts → implementation → Technical Pilot → measurements → production decisions.
Архітектурні документи = baseline/reference design, деталі уточнюються практикою.

Виконано до цього моменту: Linux-host → PostgreSQL+pgvector → Python env → embeddings → локальний inference → Hermes Agent → кілька локальних LLM → tool calling → long-context → agent tool loops → серія controller/eval тестів → вибір MamayLM як поточного кандидата.

Наступний етап: collectors/ingestion (web/news, Telegram) → raw data → normalization → provenance → content/occurrence → DB → embeddings/LLM analysis.

## 2. Апаратна платформа

- Host: MSI Vector 16 HX AI A2XWJG
- CPU: Intel Core Ultra 9 275HX
- RAM: 64 GB (~60 GiB доступно ОС)
- GPU: NVIDIA RTX 5090 Laptop, VRAM 24463 MiB, Compute Capability 12.0 (SM_120)
- Storage: 2×2TB Samsung NVMe SSD
- BIOS: E15M3IMS.10E (07/08/2025)

## 3. ОС та доступ

- Ubuntu 26.04 LTS, kernel 7.0.0-29-generic
- Windows workstation → ZeroTier → SSH → Ubuntu host; passwordless SSH ключ працює; VS Code Remote SSH працює; SSH alias на Windows: `mip`
- Робочий каталог: `~/mip`, git repo створено (branch `main`), remote поки не привʼязаний (планувався власний GitLab)
- **OS-юзер на сервері: `ovasyliev`.** Postgres-роль з таким іменем на момент 18.08 не існувала (peer-auth фейлився) — виправлено створенням `CREATE ROLE ovasyliev LOGIN` + `GRANT ALL ON DATABASE mip_dev`.

## 4. Base tools

Git 2.53.0, CMake 4.2.3, GCC 15.2.0 / GCC-13 13.4.0, Python system 3.14.4, uv 0.12.3. Ninja/Docker/NVIDIA Container Toolkit — НЕ встановлені, контейнеризація на пілоті не вводилась.

## 5. Python environment

`~/mip/.venv`, Python 3.12.13, керується через uv. Ключові пакети: vLLM 0.27.1, transformers 5.15.0, sentence-transformers 5.7.0, huggingface_hub 1.27.0, PyTorch 2.13.0+cu130 (CUDA runtime 13.0, RTX 5090 визначається коректно).

## 6. NVIDIA/CUDA

Driver 595.84 (open kernel module), nvidia-smi показує CUDA 13.2 (driver capability, не Toolkit). System CUDA Toolkit: `/usr/bin/nvcc` 12.4.131, symlink `/usr/local/cuda` існує. PyTorch використовує власний bundled runtime cu130. Системний Toolkit НЕ змінювався під PyTorch/vLLM.

## 7. GPU power-management інцидент

Hard hang в ніч 13→14.08, гіпотеза — NVIDIA idle RTD3/D3cold. Застосовано `/etc/modprobe.d/nvidia-disable-s0ix.conf`:
```
options nvidia NVreg_EnableS0ixPowerManagement=0
options nvidia NVreg_PreserveVideoMemoryAllocations=1
options nvidia NVreg_DynamicPowerManagement=0x00
```
+ `update-initramfs -u` + reboot. Після цього — стабільно, повторного hang не було. **Без нового симптому не чіпати.**

## 8. Ethernet-інцидент 15.08

Короткий disconnect, у логах `r8125 link down / lost carrier / DHCP lease lost`. Без kernel panic/Xid/OOM/NIC reset поруч. Ймовірна причина — фізичний рівень (кабель/конектор/switch), NIC/driver — другорядна гіпотеза. Мережевий стек не змінювався.

## 9-10. PostgreSQL + pgvector

PostgreSQL 18.4 (Ubuntu 18.4-0ubuntu0.26.04.1), сервіс працює, `pg_isready` → OK. Робоча БД: `mip_dev`. pgvector 0.8.1 підключений — end-to-end тест (vector column, запис embeddings, similarity search, HNSW index) пройдено. **Повторний smoke-test без нового симптому не потрібен.**

## 11. Embeddings

BAAI BGE-M3, 1024-d. Практичний тест на українських реченнях: generation → запис у pgvector → similarity query → семантично близькі речення отримали вищий similarity. Тест успішний.

## 12. R/аналітика

R 4.6.1, RStudio Server 2026.07.1 (active, remote робота перевірена). DBeaver → PostgreSQL по SSH налаштовано. Power BI виключений з Technical Pilot/MVP на Linux-host.

## 13-19. Hermes Agent + перші моделі-кандидати

Hermes Agent 0.20.1. Ключовий поточний конфіг:
```yaml
model:
  default: MamayLM
  provider: custom
  base_url: http://localhost:8000/v1
  api_mode: chat_completions
  max_tokens: 8192
agent:
  parallel_tool_call_guidance: false
terminal:
  backend: local
  cwd: .
  timeout: 180
```
`TERMINAL_CWD` deprecation warning з `.env` — косметичний, не блокує.

Протестовані й відхилені як controller:
- **Hermes-4-14B-FP8** (Qwen3-14B base) — технічно працював (~46-47 tok/s, ~20GB VRAM), відхилений: native context ~40960 < мінімум Hermes ~64000, якість/repetition/Chinese artefacts. Лишається лише як benchmark.
- **Qwen3.5-9B** — tool calling PASS, Hermes integration PASS, 64K context PASS (launch-параметри: `--gpu-memory-utilization 0.92`, `--max-num-seqs 128` — критично, 0.90 і 256 не проходили). Відхилений як controller: нестабільна українська (перемикання на РУ), mixed-script artefacts, галюцинації технічних деталей. Лишається як infrastructure/tool smoke-test модель.
- **Phi-4-mini-instruct** — розглянута і виключена без подальшого тестування.

## 20-36. MamayLM v2.0 27B — поточний кандидат

Основа: Google Gemma 3 27B, адаптація під українську. Обрано GGUF **Q4_K_M** (~16GB) під RTX 5090 24GB (Q5/Q6/Q8 варіанти теж є офіційно, але не обирались).

Файл: `~/mip/models/mamaylm-gemma3-27b-v2-q4km/MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf`

Inference backend: **llama.cpp b9642** (офіційний prebuilt, реліз 2026-06-15), каталог `~/mip/tools/llama.cpp-vulkan/llama-b9642`, backend **Vulkan** (не CUDA — щоб не чіпати system CUDA/driver). Vulkan-девайси: `Vulkan0`=Intel iGPU, `Vulkan1`=RTX 5090 — явно вказується `--device Vulkan1`.

Робочий launch (65K context, поточний Technical Pilot стандарт):
```bash
cd ~/mip/tools/llama.cpp-vulkan/llama-b9642
./llama-server \
  --model ~/mip/models/mamaylm-gemma3-27b-v2-q4km/MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M.gguf \
  --device Vulkan1 \
  --gpu-layers all \
  --ctx-size 65536 \
  --parallel 1 \
  --host 127.0.0.1 \
  --port 8000 \
  --jinja \
  --chat-template-file ~/mip/tools/chat-templates/mamaylm-gemma3-tools.jinja
```
VRAM при 65K: used ~21817 MiB, free ~2168 MiB — headroom невеликий, KV лишений у default форматі (без q8/flash-attention forcing).

**Native Gemma template не підтримує tool calling** (`supports_tools: false`) — підтверджено окремо, що це проблема template/runtime adaptation, а не weights (прямий тест з ручним Hermes-style tool prompt у system message показав, що модель вміє формувати правильний `<tool_call>`). ChatML template теж не спрацював (тож `supports_tools: false`, плюс галюцинації).

**Рішення: custom tool-aware Jinja template** — `~/mip/tools/chat-templates/mamaylm-gemma3-tools.jinja`. Зберігає native Gemma turn format, додає Hermes-style `<tools>/<tool_call>/<tool_response>`. Після підключення — `supports_tools: true`, `supports_tool_calls: true`, `supports_parallel_tool_calls: true`.

Підтверджено: structured OpenAI tool call (finish_reason: tool_calls) → tool result round-trip → коректна final відповідь без вигаданих фактів. Plain-chat regression-тест після введення custom template — не зламався.

Long-context (needle) тест: **~58K tokens реального промпту**, факт на початку контексту (`ORION-TEST code 739184`) коректно retrieved в кінці, `finish_reason: stop`, без OOM.

## 37-43. Hermes ↔ Mamay integration, dependent tool calls

Model config Hermes переключено на MamayLM (той самий local llama-server endpoint). Single tool loop, multiple independent tool calls — PASS.

**Виявлена проблема:** dependent tool calls (крок 2 залежить від результату кроку 1) — Hermes формував другий tool call ДО отримання результату першого (в результаті substituted placeholder `<TOKEN>` замість реального значення). Ізольовано: пряме API-звернення до llama.cpp (без Hermes) з `parallel_tool_calls: false` і explicit instruction — модель сама коректно серіалізувала dependent calls. Отже проблема була в Hermes-side prompt steering, не у weights.

Причина знайдена в Hermes source: universal `PARALLEL_TOOL_CALL_GUIDANCE` block застосовується до всіх моделей за замовчуванням (`agent.parallel_tool_call_guidance: true`), Gemma-specific handling в Hermes на це не розрахований для кастомного model ID `MamayLM`.

**Фікс:** `hermes config set agent.parallel_tool_call_guidance false` + повний restart Hermes. Після цього dependent tool loop (token generation → sha256 залежний виклик) пройшов коректно з реальним значенням token.

**Це налаштування — не випадкове, не повертати без нового тесту, що підтверджує іншу поведінку.**

## 44-65. Controller evals (MamayLM)

Серія synthetic tests на factual discipline / epistemic status / provenance / content-occurrence / claims / temporal reasoning / causal reasoning / tool discipline. Детальний перелік усіх 20+ тестів — у повній версії звіту (дивись історію чату при потребі, тут — підсумок).

**Підтверджені сильні сторони:** українська мова, 65K context + long-context retrieval, structured tool calling, Hermes integration, dependent tool loops (після config fix), temporal reasoning, content/occurrence reasoning, multi-claim content reasoning, event candidate grouping, history preservation (нові докази не переписують історію claim'ів), базовий provenance graph reasoning, positive/negative/ambiguous claim separation, слідування explicit operational policy.

**Повторювані слабкі місця:**
- іноді завищує epistemic status (`supported` → мало б бути `partially confirmed`)
- може перетворювати source reliability на claim probability без formal calibration
- може додумувати правдоподібний причинний механізм без явних підстав
- без explicit tool policy іноді вибирає неправильний diagnostic tool (зафіксовано конкретний epizod tool thrashing + спробу sudo — сесію після цього завершено; з жорсткою policy тул-вибір суттєво стабілізувався, приклад: `pg_isready` замість вигаданих шляхів)
- у складних semantic-задачах іноді губить provenance independence, плутає agreement/conflict з independence/dependence
- next-evidence selection іноді не враховує temporal scope

Ці недоліки **НЕ є причиною зараз міняти модель або робити fine-tuning** — наступний шар (system/controller rules, Agent Skills, DAG/loops, data contracts, evidence policies, verification, RAG, evals) має адресувати їх раніше, ніж retraining.

## 66. План поведінкового шару (Agent Skills)

Розглядається open Agent Skills standard: `skill-name/{SKILL.md, scripts/, references/, assets/}`. Плановані класи skills для МІП: fact/evidence discipline, provenance, source assessment, claim extraction, content/occurrence, event candidate analysis, technical diagnostics, database operations, collectors, Telegram, web/news parsing, RAG, verification, reporting.

## 67-69. Поточний inference stack (підсумок)

```
MamayLM-Gemma-3-27B-IT-v2.0-Q4_K_M
  → llama.cpp b9642 / Vulkan → RTX 5090
  → OpenAI-compatible API (127.0.0.1:8000/v1)
  → Hermes Agent 0.20.1
  → tools / terminal / (майбутні) skills / workflows
```
Context 65536, 1 parallel slot, Hermes max_tokens output 8192.

**Без нового симптому/виміряної потреби НЕ чіпати:** kernel, NVIDIA driver, CUDA Toolkit, GPU power-management mitigation, firmware/BIOS, network stack, PostgreSQL setup, pgvector setup, base Python env, Mamay quantization, llama.cpp backend, custom Jinja template, `parallel_tool_call_guidance`. Без практичної потреби не вводити: Docker, Kubernetes, Kafka, Redis, Celery, distributed inference, multi-node DB, зайву оркестрацію.

## 70-79. Поточний етап: collectors/ingestion

Runtime/LLM Technical Pilot завершено, старт збору реальних даних: web/news + Telegram.

Мінімальний наскрізний pipeline (орієнтир, не наказ реалізувати все одразу): source → collector → raw data → basic normalization → source/provenance/timestamps → content/occurrence handling → DB → embeddings → LLM analysis.

Відкриті питання перед реалізацією (детальний розбір і рішення — в робочому чаті collectors, не тут): ingestion contract, raw vs normalized representation, source identifiers, canonical URL, Telegram message ID, timestamps/timezone semantics, edited/deleted content, content hashing, duplicate/repost detection, content↔occurrence, idempotency, retry/rate limits, HTML changes, RSS availability, logging, processing state, reprocessing, credentials/session storage.

**Telegram:** Telethon згадувався в reference design як приклад, не як жорстка вимога — перед реалізацією перевірити актуальний стан Telegram API/client options, rate limits, session handling, edited/deleted messages, message/channel IDs, media, reply/repost/forward metadata. Credentials/session strings — ніколи в git/chat logs/debug output.

**Web/news:** пріоритет RSS/API → structured HTML → generic HTML parsing. Без browser automation там, де вистачає HTTP/RSS/HTML-парсера.

OPSEC: не виводити в чат/дебаг реальні паролі, токени, API keys, Telegram session strings, реальні IP/мережеві ідентифікатори, персональні дані, службову інформацію обмеженого доступу — тільки placeholders. Сервер фізично віддалений → пріоритет user-space/reversible/minimal-blast-radius змін.

## 76. Статус на момент передачі (чекліст)

Ubuntu/SSH/VS Code Remote/Git/Python venv/PostgreSQL/pgvector/HNSW/BGE-M3/R-RStudio/DBeaver/RTX5090 inference/llama.cpp Vulkan/MamayLM 27B/65K context/~58K practical retrieval/structured tool calling/tool-result round-trip/Hermes integration/single+multi+dependent tool loops/Ukrainian language/basic controller evals — усе **PASS**. Fine-tuning моделі — НЕ перший наступний крок, розглядається лише якщо policy/skills/orchestration виявляться недостатніми на реальних задачах.

**Наступний крок (актуальний з 18.08.2026, ведеться в окремому робочому чаті collectors):** ingestion contract → перший RSS-collector (обрано ТСН, `https://tsn.ua/rss/full.rss`, як перше джерело з міркувань обсягу для стрес-тесту dedup) → реальні записи → raw/normalized/provenance/timestamps у PostgreSQL → idempotency check → далі Telegram collector на тому самому контракті.
