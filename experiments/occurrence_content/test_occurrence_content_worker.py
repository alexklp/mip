#!/usr/bin/env python3
"""
experiments/occurrence_content/test_occurrence_content_worker.py

Два блоки:

1. TestExtractionInvariant — без DB, без мережі, лише trafilatura+lxml на
   fixture HTML. Запускається завжди.

2. DB-тести (TestPersistenceAndEligibility, TestOrchestration) — потребують
   реальну mip_dev з уже застосованою sql/036_add_occurrence_content.sql.
   Автоматично SKIP, якщо БД недоступна.

   Fixture-стратегія — свідомо БЕЗ фабрикації нових рядків sources/
   content_items/item_occurrences: я не маю впевненості щодо повної
   поточної схеми цих таблиць (contour_id та інші колонки могли додатись
   пізнішими міграціями, яких я тут не бачив) — вигадувати INSERT і
   ризикувати впасти на невідомому NOT NULL/CHECK нечесно й крихко. Тести
   натомість SELECT'ять один реальний РСС occurrence (read-only) і
   працюють з ним. Усе, що пишеться в occurrence_content:
     - TestPersistenceAndEligibility: в межах ОДНІЄЇ транзакції з rollback
       у tearDown -- жодного сліду в БД, реальний occurrence не чіпається;
     - TestOrchestration: process_occurrence() свідомо відкриває власні
       короткі з'єднання й комітить (production-контракт, не змінюється
       заради тестів) -- тому маркується заздалегідь відрізним
       extractor_version/profile_version ("TEST-*") і прибирається явним
       DELETE в tearDown за цим маркером, а не rollback'ом.

Запуск:
  cd experiments/occurrence_content
  uv run python3 test_occurrence_content_worker.py -v
"""
from __future__ import annotations

import hashlib
import unittest
import uuid
from unittest import mock

import psycopg

import occurrence_content_worker as ocw


FIXTURE_HTML = """
<html><body>
<nav>Menu Home About Contact</nav>
<article>
<h1>Головний заголовок</h1>
<p>Перший абзац з важливим фактом номер один.</p>
<h2>Підзаголовок</h2>
<ul><li>Пункт перший</li><li>Пункт другий</li></ul>
<p>Другий абзац статті.</p>
</article>
<footer>Copyright 2026</footer>
<div class="comments"><p>Коментар читача, не має бути в тексті</p></div>
</body></html>
"""

EMPTY_HTML = "<html><body><nav>Menu only, no article</nav></body></html>"


# ---------------------------------------------------------------------------
# 1. Extraction invariant -- без DB, без мережі
# ---------------------------------------------------------------------------

class TestExtractionInvariant(unittest.TestCase):

    def test_single_bare_extraction_call(self):
        """extract_once() робить РІВНО один виклик bare_extraction() --
        пряме підтвердження single-pass контракту (claude/26 rev.2, п.1/6.1),
        не побічний доказ через порівняння вмісту."""
        with mock.patch.object(
            ocw.trafilatura, "bare_extraction", side_effect=ocw.trafilatura.bare_extraction
        ) as spy:
            result = ocw.extract_once(FIXTURE_HTML, "https://example.com/a")
        self.assertEqual(spy.call_count, 1)
        self.assertIsNotNone(result)

    def test_text_and_structured_content_derive_from_same_tree(self):
        """text_content і XML-серіалізація ТОГО САМОГО дерева (без pretty-print,
        щоб уникнути whitespace-артефактів round-trip serialization) дають
        ідентичний текст після повторного xmltotxt -- підтверджує, що обидва
        поля справді походять з одного extraction pass."""
        document = ocw.trafilatura.bare_extraction(
            FIXTURE_HTML, url="https://example.com/a", output_format="python",
            **ocw.EXTRACTION_PROFILE_KWARGS,
        )
        result = ocw.extract_once(FIXTURE_HTML, "https://example.com/a")

        text_from_same_tree = ocw.xmltotxt(document.body, False)
        self.assertEqual(result["text_content"], text_from_same_tree)

        no_pretty_xml = ocw.etree.tostring(document.body, pretty_print=False, encoding="unicode")
        reparsed = ocw.etree.fromstring(no_pretty_xml.encode("utf-8"))
        self.assertEqual(ocw.xmltotxt(reparsed, False), result["text_content"])

    def test_comments_and_nav_excluded_from_text(self):
        result = ocw.extract_once(FIXTURE_HTML, "https://example.com/a")
        self.assertNotIn("Коментар читача", result["text_content"])
        self.assertNotIn("Menu Home About Contact", result["text_content"])

    def test_structure_preserved_in_structured_content(self):
        result = ocw.extract_once(FIXTURE_HTML, "https://example.com/a")
        self.assertIn("<head", result["structured_content"])
        self.assertIn("<list", result["structured_content"])
        self.assertEqual(result["structured_content_format"], "trafilatura_xml")

    def test_text_hash_matches_text_content(self):
        result = ocw.extract_once(FIXTURE_HTML, "https://example.com/a")
        expected = hashlib.sha256(result["text_content"].encode("utf-8")).hexdigest()
        self.assertEqual(result["text_hash"], expected)

    def test_empty_extraction_returns_none(self):
        result = ocw.extract_once(EMPTY_HTML, "https://example.com/empty")
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# DB fixture base -- read-only SELECT реального RSS occurrence, без фабрикації
# ---------------------------------------------------------------------------

TEST_EXTRACTOR_VERSION = "TEST-0.0.0"
TEST_PROFILE_VERSION = "TEST"


def _require_real_rss_occurrence(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT io.occurrence_id, io.external_ref
            FROM item_occurrences io
            JOIN sources s ON s.source_id = io.source_id
            WHERE s.source_type = 'rss'
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        raise unittest.SkipTest("немає жодного RSS occurrence в БД для fixture")
    return row


class DBRollbackTestCase(unittest.TestCase):
    """Один реальний RSS occurrence (read-only), усі occurrence_content
    записи -- в межах транзакції з rollback у tearDown."""

    @classmethod
    def setUpClass(cls):
        try:
            probe = psycopg.connect(ocw.DB_DSN)
        except Exception as exc:
            raise unittest.SkipTest(f"mip_dev недоступна: {exc}")
        try:
            with probe.cursor() as cur:
                cur.execute("SELECT to_regclass('occurrence_content')")
                if cur.fetchone()[0] is None:
                    raise unittest.SkipTest(
                        "таблиця occurrence_content відсутня -- застосуй "
                        "sql/036_add_occurrence_content.sql"
                    )
        finally:
            probe.close()

    def setUp(self):
        self.conn = psycopg.connect(ocw.DB_DSN)
        self.conn.autocommit = False
        self.occurrence_id, self.external_ref = _require_real_rss_occurrence(self.conn)

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def insert(self, **kwargs):
        kwargs.setdefault("extractor_version", TEST_EXTRACTOR_VERSION)
        kwargs.setdefault("profile_version", TEST_PROFILE_VERSION)
        kwargs.setdefault("code_revision", "test")
        kwargs.setdefault("fetch_url", self.external_ref)
        kwargs.setdefault("occurrence_id", self.occurrence_id)
        return ocw.insert_attempt(self.conn, **kwargs)

    def full_success_kwargs(self, **overrides):
        base = dict(
            status="success",
            text_content="text",
            text_hash="hash",
            structured_content="<body/>",
            structured_content_format="trafilatura_xml",
            final_url=self.external_ref,
        )
        base.update(overrides)
        return base


# ---------------------------------------------------------------------------
# 2. DDL constraints, attempt_no, append-only, eligibility, resolver
# ---------------------------------------------------------------------------

class TestPersistenceAndEligibility(DBRollbackTestCase):

    def test_next_attempt_no_starts_at_1(self):
        n = ocw.compute_next_attempt_no(
            self.conn, self.occurrence_id,
            extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
        )
        self.assertEqual(n, 1)

    def test_next_attempt_no_increments_after_insert(self):
        self.insert(attempt_no=1, **self.full_success_kwargs())
        n = ocw.compute_next_attempt_no(
            self.conn, self.occurrence_id,
            extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
        )
        self.assertEqual(n, 2)

    def test_next_attempt_no_independent_per_identity(self):
        """Інша extractor_version -- незалежний рахунок з 1 (claude/26 п.1)."""
        self.insert(attempt_no=1, **self.full_success_kwargs())
        n = ocw.compute_next_attempt_no(
            self.conn, self.occurrence_id,
            extractor_version="TEST-9.9.9", profile_version=TEST_PROFILE_VERSION,
        )
        self.assertEqual(n, 1)

    def test_append_only_error_then_success_both_preserved(self):
        """Ретрай після помилки НЕ перезаписує попередню спробу -- обидва
        рядки лишаються (claude/26 п.0, підтверджений claim_extraction_runs
        precedent)."""
        self.insert(attempt_no=1, status="fetch_error", errors=["boom"])
        self.insert(attempt_no=2, **self.full_success_kwargs())

        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_no, status FROM occurrence_content "
                "WHERE occurrence_id = %s AND extractor_version = %s "
                "ORDER BY attempt_no",
                (self.occurrence_id, TEST_EXTRACTOR_VERSION),
            )
            rows = cur.fetchall()
        self.assertEqual(rows, [(1, "fetch_error"), (2, "success")])

    def test_unique_constraint_blocks_duplicate_attempt(self):
        self.insert(attempt_no=1, status="fetch_error", errors=["x"])
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.conn.transaction():
                self.insert(attempt_no=1, status="fetch_error", errors=["y"])

    def test_success_check_requires_full_bundle(self):
        """CHECK на success вимагає text_content+text_hash+structured_content+
        structured_content_format+final_url разом -- не лише text_content
        (claude/26 rev.2, п.4: старий слабкий CHECK виправлено)."""
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.conn.transaction():
                self.insert(
                    attempt_no=1, status="success",
                    text_content="text", text_hash=None,
                    structured_content=None, structured_content_format=None,
                    final_url=self.external_ref,
                )

    def test_error_status_allows_null_content_fields(self):
        # не мусить кинути виняток
        self.insert(attempt_no=1, status="fetch_error", errors=["network down"])
        self.insert(attempt_no=2, status="extraction_error", errors=["empty body"],
                     final_url=self.external_ref, http_status=200, raw_html="<html></html>")

    def test_eligible_when_no_prior_attempts(self):
        rows = ocw.fetch_eligible_occurrences(
            self.conn, extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
            occurrence_id=self.occurrence_id, max_attempts=3, limit=10,
        )
        self.assertEqual([r[0] for r in rows], [self.occurrence_id])

    def test_ineligible_after_success(self):
        self.insert(attempt_no=1, **self.full_success_kwargs())
        rows = ocw.fetch_eligible_occurrences(
            self.conn, extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
            occurrence_id=self.occurrence_id, max_attempts=3, limit=10,
        )
        self.assertEqual(rows, [])

    def test_retry_limit_respected(self):
        """error retryable до max_attempts; на межі -- виключається."""
        for n in (1, 2):
            self.insert(attempt_no=n, status="fetch_error", errors=["x"])
        rows = ocw.fetch_eligible_occurrences(
            self.conn, extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
            occurrence_id=self.occurrence_id, max_attempts=3, limit=10,
        )
        self.assertEqual([r[0] for r in rows], [self.occurrence_id], "2 помилки < max_attempts=3 -- ще eligible")

        self.insert(attempt_no=3, status="fetch_error", errors=["x"])
        rows = ocw.fetch_eligible_occurrences(
            self.conn, extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
            occurrence_id=self.occurrence_id, max_attempts=3, limit=10,
        )
        self.assertEqual(rows, [], "3 помилки == max_attempts=3 -- вже НЕ eligible")

    def test_rss_only_filter(self):
        """source_type фільтр застосований -- запит з чужим source_type не
        поверне наш RSS occurrence, навіть якщо він інакше eligible."""
        rows = ocw.fetch_eligible_occurrences(
            self.conn, extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
            occurrence_id=self.occurrence_id, source_type="telegram", max_attempts=3, limit=10,
        )
        self.assertEqual(rows, [])

    def test_resolver_returns_latest_success_within_identity(self):
        self.insert(attempt_no=1, **self.full_success_kwargs(text_content="v1 text"))
        self.insert(attempt_no=2, **self.full_success_kwargs(text_content="v2 text"))

        row = ocw.resolve_current_occurrence_content(
            self.conn, self.occurrence_id,
            extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
        )
        self.assertEqual(row["attempt_no"], 2)
        self.assertEqual(row["text_content"], "v2 text")

    def test_resolver_ignores_other_identity_even_if_newer(self):
        """Регресійний тест на баг з ревізії 1: діагностичний ре-ран СТАРІШОЇ
        identity, записаний ПІЗНІШЕ за часом, не повинен перебити resolver
        для identity, яку явно запитує споживач (claude/26 rev.2, п.2)."""
        self.insert(attempt_no=1, extractor_version=TEST_EXTRACTOR_VERSION,
                     **self.full_success_kwargs(text_content="current identity text"))
        # "пізніший" запис іншої identity -- інший extractor_version
        self.insert(attempt_no=1, extractor_version="TEST-0.0.1-OLD-RERUN",
                     **self.full_success_kwargs(text_content="stale rerun text"))

        row = ocw.resolve_current_occurrence_content(
            self.conn, self.occurrence_id,
            extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
        )
        self.assertEqual(row["text_content"], "current identity text")

    def test_resolver_returns_none_when_no_success(self):
        self.insert(attempt_no=1, status="fetch_error", errors=["x"])
        row = ocw.resolve_current_occurrence_content(
            self.conn, self.occurrence_id,
            extractor_version=TEST_EXTRACTOR_VERSION, profile_version=TEST_PROFILE_VERSION,
        )
        self.assertIsNone(row)


# ---------------------------------------------------------------------------
# 3. Orchestration (process_occurrence) -- реальні короткі connections/commits,
#    прибирається явним DELETE за унікальним test-маркером, не rollback'ом.
# ---------------------------------------------------------------------------

class TestOrchestration(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            probe = psycopg.connect(ocw.DB_DSN)
        except Exception as exc:
            raise unittest.SkipTest(f"mip_dev недоступна: {exc}")
        try:
            row = _require_real_rss_occurrence(probe)
        finally:
            probe.close()
        cls.occurrence_id, cls.external_ref = row

    def setUp(self):
        self.marker = f"TEST-ORCH-{uuid.uuid4().hex[:8]}"
        self.profile = "TEST"
        # НЕ mock.patch.object(ocw, "EXTRACTOR_VERSION", ...): Python зв'язує
        # значення default-параметра під час визначення функції, тож патч
        # модульної константи НЕ впливає на вже визначені сигнатури
        # process_occurrence()/insert_attempt() -- знайдено прогоном тестів
        # проти реальної Postgres (перші версії цього тесту мовчки писали в
        # РЕАЛЬНУ identity замість тестового маркера). Identity тепер
        # передається explicit keyword-аргументом у кожен виклик нижче.

    def tearDown(self):
        conn = psycopg.connect(ocw.DB_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM occurrence_content WHERE extractor_version = %s",
                    (self.marker,),
                )
            conn.commit()
        finally:
            conn.close()

    def test_fetch_error_persisted(self):
        with mock.patch.object(
            ocw, "fetch_article", side_effect=ocw.requests.exceptions.ConnectionError("no route")
        ):
            result = ocw.process_occurrence(
                self.occurrence_id, self.external_ref, "test-rev",
                extractor_version=self.marker, profile_version=self.profile,
            )
        self.assertEqual(result, "fetch_error")

        conn = psycopg.connect(ocw.DB_DSN)
        try:
            row = ocw.resolve_current_occurrence_content(
                conn, self.occurrence_id, extractor_version=self.marker, profile_version="TEST",
            )
            self.assertIsNone(row, "fetch_error не повинен бути видимий resolver'у як success")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM occurrence_content WHERE occurrence_id=%s AND extractor_version=%s",
                    (self.occurrence_id, self.marker),
                )
                self.assertEqual(cur.fetchall(), [("fetch_error",)])
        finally:
            conn.close()

    def test_extraction_error_persisted(self):
        with mock.patch.object(
            ocw, "fetch_article", return_value=(self.external_ref, 200, EMPTY_HTML)
        ):
            result = ocw.process_occurrence(
                self.occurrence_id, self.external_ref, "test-rev",
                extractor_version=self.marker, profile_version=self.profile,
            )
        self.assertEqual(result, "extraction_error")

        conn = psycopg.connect(ocw.DB_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status, text_content FROM occurrence_content "
                    "WHERE occurrence_id=%s AND extractor_version=%s",
                    (self.occurrence_id, self.marker),
                )
                rows = cur.fetchall()
            self.assertEqual(rows, [("extraction_error", None)])
        finally:
            conn.close()

    def test_success_persisted_and_terminal(self):
        with mock.patch.object(
            ocw, "fetch_article", return_value=(self.external_ref, 200, FIXTURE_HTML)
        ):
            result = ocw.process_occurrence(
                self.occurrence_id, self.external_ref, "test-rev",
                extractor_version=self.marker, profile_version=self.profile,
            )
        self.assertEqual(result, "success")

        conn = psycopg.connect(ocw.DB_DSN)
        try:
            row = ocw.resolve_current_occurrence_content(
                conn, self.occurrence_id, extractor_version=self.marker, profile_version="TEST",
            )
            self.assertIsNotNone(row)
            self.assertIn("Головний заголовок", row["text_content"])

            # success termінальний: eligibility запит більше не бачить occurrence
            eligible = ocw.fetch_eligible_occurrences(
                conn, extractor_version=self.marker, profile_version="TEST",
                occurrence_id=self.occurrence_id, max_attempts=3, limit=10,
            )
            self.assertEqual(eligible, [])
        finally:
            conn.close()

    def test_retry_after_error_gets_attempt_2(self):
        with mock.patch.object(
            ocw, "fetch_article", side_effect=ocw.requests.exceptions.ConnectionError("no route")
        ):
            ocw.process_occurrence(
                self.occurrence_id, self.external_ref, "test-rev",
                extractor_version=self.marker, profile_version=self.profile,
            )

        with mock.patch.object(
            ocw, "fetch_article", return_value=(self.external_ref, 200, FIXTURE_HTML)
        ):
            result = ocw.process_occurrence(
                self.occurrence_id, self.external_ref, "test-rev",
                extractor_version=self.marker, profile_version=self.profile,
            )
        self.assertEqual(result, "success")

        conn = psycopg.connect(ocw.DB_DSN)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT attempt_no, status FROM occurrence_content "
                    "WHERE occurrence_id=%s AND extractor_version=%s ORDER BY attempt_no",
                    (self.occurrence_id, self.marker),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(1, "fetch_error"), (2, "success")])


if __name__ == "__main__":
    unittest.main()
