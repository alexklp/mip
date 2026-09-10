# МІП Web MVP v1

Status: working baseline / reference design
Date: 2026-09-04

## 1. Purpose

Web MVP v1 is not a temporary static demo and not a throwaway prototype. It is the first working web version of МІП that will be developed incrementally and used for:

- daily development and verification;
- demonstrations over VКЗ;
- work by a small group of testers;
- human annotation / verification of analytical results;
- gradual addition of production functionality.

The target is to evolve the same application into the operational AРМ. When production access is ready, the application should not be rewritten: only the external access / ingress mechanism changes.

Development/test access:

`browser -> SSH tunnel or temporary authenticated ngrok -> MІП Web -> PostgreSQL / analytical pipeline`

Target production access:

`browser AРМ -> approved domain / VPN / internal network -> same MІП Web -> same backend and data model`

## 2. Core architectural principles

1. **Production-shaped from the start.** The web application is a real service, not generated HTML.
2. **Live data.** Screens read current PostgreSQL data. Snapshot JSON/HTML from the old demo may be used only as a visual/reference source, not as the data architecture of the new application.
3. **Existing analytical pipeline stays separate.** Collectors, normalization, occurrence/full-text extraction, segmentation, embeddings, routing, claim extraction, relations and later event logic remain background/server-side components. The web layer does not duplicate this logic.
4. **Web is primarily an operator interface.** It exposes system state, analytical results, provenance/evidence and human actions.
5. **Human decisions are first-class data.** Annotation/verification results must be written to PostgreSQL with actor identity, time and decision provenance; they must not live in local JSON files.
6. **Traceability is mandatory.** Important analytical conclusions must remain navigable back to source occurrence, publication/full text, segment, claim and evidence.
7. **Long LLM work is not coupled to normal page requests.** Mamay and other expensive analytical tasks remain serialized/background work; the UI may later request or observe such work through explicit bounded mechanisms.
8. **Simple and reversible implementation first.** Do not add frontend frameworks, orchestration or infrastructure until the actual UI/use case requires them.

## 3. Initial technology baseline

Working baseline for Web MVP v1:

- **Backend:** FastAPI / ASGI
- **HTML rendering:** Jinja2
- **Interactive UI:** HTMX + small amount of ordinary JavaScript where needed
- **Database:** existing PostgreSQL (`mip_dev` during development)
- **Python environment / execution:** existing `uv` project workflow
- **Development bind:** localhost only on `srv01`
- **Developer access:** SSH port-forward
- **Small external test/demo access:** authenticated ngrok tunnel
- **Production ingress:** deferred; later replace tunnel with approved domain/VPN/internal-network access without rewriting the application

React/Vue or a separate SPA are not part of v1 unless a concrete UI requirement later justifies them.

## 4. Functional scope

The web application should grow by working vertical slices rather than by building all screens at once.

### v1 first vertical slice

`Overview -> Materials -> Material detail -> Segments -> Claims`

A user must be able to:

- open the current material stream;
- filter by time/source/monitoring contour where supported by current data;
- open one publication/occurrence;
- see its full extracted text where available;
- see analytical segments;
- see claims extracted from a segment;
- see claim evidence/provenance and source context.

This slice is read-oriented and proves that the live pipeline can be inspected through a real AРМ.

### next slice

`Relations -> Verification / Annotation`

The user should be able to:

- inspect candidate/verified relations between claims;
- see both source contexts and exact provenance;
- accept/reject/label a review item according to the active contract;
- save a human decision;
- identify who made the decision and when;
- reopen the stored result later.

### later slices

Only after the previous slices are stable:

- canonical events / event trace;
- thematic/topic views;
- dynamics and monitoring summaries;
- operational status / pipeline health where useful;
- search and additional analyst tools.

The existing static demo/reporting code may donate useful visual ideas (sidebar, KPI cards, topic/event cards, etc.), but it is not the implementation base of the new service.

## 5. Access and user identity

During development the application remains bound to localhost on the server.

For the developer, SSH tunneling is sufficient.

For a small group of testers or leadership, a temporary authenticated ngrok endpoint is acceptable for non-restricted demo/test data. This is a temporary ingress mechanism, not production infrastructure.

Before write-capable annotation is used by multiple people, each human action must have a reliable actor identity. The exact authentication mechanism is an implementation detail to select before that slice; a shared anonymous credential is insufficient for attribution of annotation decisions.

No real passwords, tokens, private network identifiers or restricted data are committed to the repository.

## 6. Web / pipeline boundary

The web service may directly perform bounded PostgreSQL reads and transactional human-review writes through dedicated application code.

It should not:

- reimplement collectors or analytical workers;
- run global backlog scans from an HTTP request;
- launch parallel Mamay calls;
- bypass existing provenance/contracts;
- silently mutate analytical outputs to make the UI look cleaner.

If a future UI action needs recalculation or LLM work, it should call an explicit bounded job/worker interface and expose status/result separately from the request lifecycle.

## 7. Data source policy

Web MVP v1 uses the current database and current implementation contracts as the source of truth.

For implementation detail, newer verified tables/workers/tests take priority over older demo/reporting assumptions.

In particular, the UI must preserve the distinction between:

`source occurrence -> occurrence/full-text representation -> segment -> claim -> relation -> event`

A segment is not an event, a lexical content item is not an occurrence, and vector similarity is not a confirmed relation.

## 8. Definition of the first usable Web MVP

The first usable milestone is reached when:

1. the FastAPI application runs on `srv01` bound to localhost;
2. it opens from the developer workstation through SSH port-forward;
3. it displays current PostgreSQL data, not a frozen demo snapshot;
4. a user can navigate from a material to its full text/segment/claims/evidence;
5. errors are visible and do not silently falsify the UI;
6. the same service can be exposed temporarily through authenticated ngrok for a small test/demo group without code changes.

That milestone is sufficient to begin iterative UI work and tester feedback. Production ingress, full IAM, HA, external deployment automation and large-scale frontend architecture are explicitly deferred until measured need appears.

## 9. Immediate implementation direction

Start with the smallest real application skeleton and one end-to-end live page flow. Do not port the old report wholesale.

Recommended implementation order:

1. FastAPI application skeleton + health endpoint + base Jinja layout.
2. PostgreSQL read layer using the existing project DSN/config approach.
3. Materials list from live data.
4. Material detail with occurrence/full-text provenance.
5. Segment and claim display with evidence.
6. Only after the read path works: relation review / annotation write path and per-user attribution.

This document is a working baseline, not a frozen architecture. Details may change when implementation or testing shows a simpler or more correct solution, while preserving the principles above.

## 10. Сигнали: Phase 4, окремий snapshot read path

Для `/signals` збережено виняток до прямого live-читання web з розділу 2:
PostgreSQL SELECT-only адаптер формує JSON `signals/4`, а HTTP-запит читає лише
локальний валідований snapshot. HTTP не запускає recall, модель або DB-запити.
Інші routes не змінено в Phase 4.

У recall допускається лише останній `content_routing_decisions.decision='analyze'`.
`maybe`, `skip` та відсутнє рішення виключені; routing coverage показано окремо
для кожного вікна та простору. Contours не є hard gate. Anchors відбираються
детермінованим round-robin між вибраними групами з одним загальним лімітом.

Core-поріг 0.18 об'єднує матеріали лише за complete-link правилом. Related
зв'язки `(0.18, 0.36]` показуються окремо для review і не змінюють кількості чи
динаміку core. Кандидат має щонайменше два distinct source_id; повтори одного
матеріалу в одному джерелі подавляються. Same-space multi-source кандидати
дозволені. Пріоритет показу: cross_space, source_count, content_count, свіжість,
стабільний ID; default display limit 20. Усі зв'язки — candidate/unverified.

Стани missing/invalid/stale/empty/ready, chronology, безпечні посилання та
автооновлення HTML збережено. `signals/2` діагностичний JSON не є результатом
Phase 4; новий validator відхиляє його. До окремо дозволеної оператором генерації
нового `signals/4` loader може показувати invalid. Файл не переписувався.

На 2026-09-08: 85 offline tests PASS, включно з FastAPI TestClient та Jinja,
без запуску сервера. Один read-only benchmark: 7 core candidates, 100 related
review links, anchors 20/20, critical_incomplete=false, RC=0. На новому common
watermark у current/ua_space виявлено 22 missing routing; вони виключені.
Новий production snapshot і розклад не створювалися. Команди, вимірювання,
параметри та обмеження recall наведено в [Signals_v1.md](Signals_v1.md).
