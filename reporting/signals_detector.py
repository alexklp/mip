"""Детермінований recall кандидатів без семантичних вердиктів."""

from dataclasses import dataclass
from datetime import datetime
import math

from reporting.signals_data import SPACES, ROUTING_DECISIONS, SignalData, windows, validate_routing_coverage


ALGORITHM_VERSION = "signals-routing-core-review/4"
MERGE_ALGORITHM_VERSION = "signals-routing-core-merge-review/6"
MERGE_MIN_MEMBER_COVERAGE = 0.60


@dataclass(frozen=True)
class RecallConfig:
    max_distance: float = 0.18
    distance_bands: tuple[float, ...] = ()
    max_contents: int = 1000
    max_pairs: int = 2000
    max_evidence: int = 12
    evidence_chars: int = 600
    related_distance: float = 0.36
    max_related_links: int = 100
    display_limit: int = 20
    merge_distance: float | None = None
    merge_min_cross_links: int = 2

    def validate(self) -> None:
        values = (self.max_distance, self.related_distance, *self.distance_bands)
        if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 2 for v in values):
            raise ValueError("Cosine distance має бути в межах [0, 2]")
        if self.related_distance < self.max_distance:
            raise ValueError("Related threshold не може бути меншим за core")
        if self.merge_distance is not None:
            if (
                type(self.merge_distance) not in (int, float)
                or not math.isfinite(self.merge_distance)
                or not self.max_distance <= self.merge_distance <= self.related_distance
            ):
                raise ValueError("Merge threshold має бути між core та related")
            if (
                type(self.merge_min_cross_links) is not int
                or not 2 <= self.merge_min_cross_links <= 100
            ):
                raise ValueError("Некоректна кількість cross-links для merge")
        if tuple(sorted(set(self.distance_bands))) != self.distance_bands:
            raise ValueError("Межі distance bands мають строго зростати")
        for name, ceiling in (("display_limit", 200), ("max_related_links", 1000), ("max_pairs", 200000), ("max_evidence", 200), ("evidence_chars", 2000), ("max_contents", 50000)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ValueError("Некоректний ліміт " + name)

    def band(self, distance: float) -> str:
        """band_0 включає першу межу; останній band не має верхньої межі."""
        return f"band_{sum(distance > edge for edge in self.distance_bands)}"


def algorithm_version(config: RecallConfig) -> str:
    """Версія алгоритму залежить від фактично увімкненого grouping contract."""
    return (
        MERGE_ALGORITHM_VERSION
        if config.merge_distance is not None
        else ALGORITHM_VERSION
    )


def cosine_distance(left: tuple, right: tuple) -> float:
    a, b = math.hypot(*left), math.hypot(*right)
    similarity = math.fsum((x / a) * (y / b) for x, y in zip(left, right))
    return max(0.0, min(2.0, 1.0 - similarity))


def counts(rows) -> dict:
    return {
        "occurrence_count": len(rows),
        "content_count": len({r.content_id for r in rows}),
        "source_count": len({r.source_id for r in rows}),
    }


def _merge_supported_groups(
    groups,
    pair_distances,
    selected,
    *,
    merge_distance: float,
    min_cross_links: int,
):
    """Об'єднуємо strict cores лише за щільною взаємною підтримкою.

    Для merge потрібні:
    - щонайменше min_cross_links між двома strict cores;
    - cross-links мають охоплювати щонайменше 60% content
      з кожного core;
    - при об'єднанні компонентів кожна пара strict cores між
      компонентами повинна мати таку підтримку.

    Це не дозволяє транзитивному ланцюжку A-B-C створити один
    giant component, якщо A та C безпосередньо не підтримані.
    """
    if not groups:
        return []

    if (
        type(merge_distance) not in (int, float)
        or not math.isfinite(merge_distance)
        or not 0 <= merge_distance <= 2
    ):
        raise ValueError("Некоректний merge distance")

    if type(min_cross_links) is not int or min_cross_links < 2:
        raise ValueError(
            "Core merge потребує щонайменше двох cross-links"
        )

    group_of = {
        cid: group_index
        for group_index, group in enumerate(groups)
        for cid in group
    }

    support = {}

    for (left, right), distance_value in pair_distances.items():
        if distance_value > merge_distance:
            continue

        left_group = group_of.get(left)
        right_group = group_of.get(right)

        if (
            left_group is None
            or right_group is None
            or left_group == right_group
        ):
            continue

        if left_group < right_group:
            edge = (left_group, right_group)
            left_member = left
            right_member = right
        else:
            edge = (right_group, left_group)
            left_member = right
            right_member = left

        row = support.setdefault(
            edge,
            {
                "links": 0,
                "left_members": set(),
                "right_members": set(),
            },
        )

        row["links"] += 1
        row["left_members"].add(left_member)
        row["right_members"].add(right_member)

    supported_edges = set()

    for (left_group, right_group), row in support.items():
        left_coverage = (
            len(row["left_members"])
            / len(groups[left_group])
        )
        right_coverage = (
            len(row["right_members"])
            / len(groups[right_group])
        )

        if (
            row["links"] >= min_cross_links
            and left_coverage >= MERGE_MIN_MEMBER_COVERAGE
            and right_coverage >= MERGE_MIN_MEMBER_COVERAGE
        ):
            supported_edges.add(
                (left_group, right_group)
            )

    parent = list(range(len(groups)))
    core_members = {
        index: {index}
        for index in range(len(groups))
    }

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left_root, right_root):
        if left_root > right_root:
            left_root, right_root = right_root, left_root

        parent[right_root] = left_root
        core_members[left_root].update(
            core_members.pop(right_root)
        )

    for left_group, right_group in sorted(supported_edges):
        left_root = find(left_group)
        right_root = find(right_group)

        if left_root == right_root:
            continue

        left_component = core_members[left_root]
        right_component = core_members[right_root]

        complete_support = all(
            tuple(sorted((left_core, right_core)))
            in supported_edges
            for left_core in left_component
            for right_core in right_component
        )

        if complete_support:
            union(left_root, right_root)

    components = {}

    for group_index, group in enumerate(groups):
        root = find(group_index)
        components.setdefault(root, []).extend(group)

    selected_order = {
        cid: index
        for index, cid in enumerate(selected)
    }

    return [
        sorted(
            members,
            key=lambda cid: selected_order[cid],
        )
        for _, members in sorted(
            components.items(),
            key=lambda item: min(
                selected_order[cid]
                for cid in item[1]
            ),
        )
    ]


def detect(data: SignalData, *, as_of: datetime, config: RecallConfig) -> dict:
    """Двоступеневий recall: strict complete-link cores + opt-in supported merge.

    Відбір за свіжістю та спостережуваністю; content hash розв'язує рівність.
    Production використовує SQL-пари; локальний fallback має бюджет.
    Невідома відстань не створює зв'язку.
    """
    data.validate()
    config.validate()
    bounds = windows(as_of)
    sources = {s.source_id: s for s in data.sources}
    contents = {c.content_id: c for c in data.contents}
    observed = {name: [] for name in bounds}
    outside = 0
    for row in data.occurrences:
        name = next((name for name, (start, end) in bounds.items() if start <= row.collected_at < end), None)
        if name is None:
            outside += 1
        else:
            observed[name].append(row)
    coverage = {}
    routing_coverage = {name: {space: dict.fromkeys(ROUTING_DECISIONS, 0) for space in SPACES} for name in bounds}
    included = []
    for name, rows in observed.items():
        coverage[name] = {}
        for space in (*SPACES, "excluded"):
            subset = [r for r in rows if (sources[r.source_id].source_group == space if space in SPACES else sources[r.source_id].source_group not in SPACES)]
            coverage[name][space] = {
                **counts(subset),
                "missing_published_at": sum(r.published_at is None for r in subset),
                "missing_embedding_content_count": len({r.content_id for r in subset if not (contents[r.content_id].embedding is not None or contents[r.content_id].has_embedding)}),
            }
            if space in SPACES:
                for cid in {r.content_id for r in subset}:
                    routing_coverage[name][space][contents[cid].routing_decision or "missing"] += 1
                included.extend(
                    r
                    for r in subset
                    if contents[r.content_id].routing_decision
                    in {"analyze", "maybe"}
                )
    if data.coverage is not None:
        coverage = data.coverage
    if data.routing_coverage is not None:
        routing_coverage = data.routing_coverage
    validate_routing_coverage(routing_coverage, coverage)
    observations = {}
    for row in included:
        observations.setdefault(row.content_id, []).append(row)
    current_start, current_end = bounds["current"]
    exact_current = bool(
        data.selection
        and data.selection.get("strategy") == "exact_current_24h"
    )

    if exact_current:
        eligible_ids = sorted(
            (
                cid
                for cid, rows in observations.items()
                if any(
                    current_start <= r.collected_at < current_end
                    for r in rows
                )
            ),
            key=lambda cid: (
                -max(
                    r.collected_at.timestamp()
                    for r in observations[cid]
                    if current_start <= r.collected_at < current_end
                ),
                -len({
                    r.source_id
                    for r in observations[cid]
                    if current_start <= r.collected_at < current_end
                }),
                contents[cid].selection_key or cid,
                cid,
            ),
        )
    else:
        eligible_ids = sorted(
            observations,
            key=lambda cid: (
                -max(
                    r.collected_at.timestamp()
                    for r in observations[cid]
                ),
                -len({
                    r.source_id
                    for r in observations[cid]
                }),
                contents[cid].selection_key or cid,
                cid,
            ),
        )
    selected = eligible_ids[:config.max_contents]
    truncated = data.truncated or len(selected) < len(eligible_ids)
    pair_distances = {}
    exhausted = False
    if data.pairs is not None:
        if len(data.pairs) > config.max_pairs:
            raise ValueError("SQL-пари перевищують бюджет detector")
        for left, right, value in data.pairs:
            pair_distances[tuple(sorted((left, right)))] = value
    else:
        comparisons = 0
        for index, left in enumerate(selected):
            for right in selected[index + 1:]:
                if comparisons >= config.max_pairs:
                    exhausted = True
                    break
                comparisons += 1
                a, b = contents[left].embedding, contents[right].embedding
                if a is not None and b is not None:
                    pair_distances[tuple(sorted((left, right)))] = cosine_distance(a, b)
            if exhausted:
                break
    def distance(left, right):
        if left == right:
            return 0.0 if contents[left].embedding is not None or contents[left].has_embedding else None
        return pair_distances.get(tuple(sorted((left, right))))
    groups = []
    membership = {}
    adjacency = {}
    for (left, right), value in pair_distances.items():
        if value <= config.max_distance:
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
    for cid in selected:
        possible = sorted({membership[other] for other in adjacency.get(cid, ()) if other in membership})
        target = next((groups[index] for index in possible if all(
            distance(cid, other) is not None and distance(cid, other) <= config.max_distance
            for other in groups[index])), None)
        if target is None:
            membership[cid] = len(groups)
            groups.append([cid])
        else:
            membership[cid] = membership[target[0]]
            target.append(cid)

    strict_groups = [list(group) for group in groups]

    if config.merge_distance is not None:
        groups = _merge_supported_groups(
            strict_groups,
            pair_distances,
            selected,
            merge_distance=config.merge_distance,
            min_cross_links=config.merge_min_cross_links,
        )

    final_membership = {
        cid: group_index
        for group_index, group in enumerate(groups)
        for cid in group
    }

    truncated = truncated or exhausted
    candidates = []
    suppressed = dict(
        singleton_single_source=0,
        repeated_content_single_source=0,
        core_single_source=0,
        previous_only=0,
    )
    for group in groups:
        representative = group[0]
        rows = sorted(
            (r for cid in group for r in observations[cid]),
            key=lambda r: (r.collected_at, r.occurrence_id),
        )
        current_rows = [
            r
            for r in rows
            if current_start <= r.collected_at < current_end
        ]
        candidate_rows = rows
        publication_rows = (
            current_rows
            if exact_current
            else candidate_rows
        )
        last_observed_rows = (
            current_rows
            if exact_current
            else candidate_rows
        )

        chronology_rows = sorted(
            candidate_rows,
            key=lambda r: (
                r.published_at or r.collected_at,
                r.collected_at,
                r.occurrence_id,
            ),
        )

        published_times = [
            r.published_at
            for r in candidate_rows
            if r.published_at is not None
        ]
        first_published_at = (
            min(published_times).isoformat()
            if published_times
            else None
        )
        last_published_at = (
            max(published_times).isoformat()
            if published_times
            else None
        )

        if len({r.source_id for r in candidate_rows}) < 2:
            reason = (
                "singleton_single_source"
                if len(candidate_rows) == 1
                else "repeated_content_single_source"
                if len(group) == 1
                else "core_single_source"
            )
            suppressed[reason] += 1
            continue

        # Signals page має current-24h contract.
        # Previous window залишається support/evidence для current signal,
        # але previous-only group не є поточним сигналом.
        if not current_rows:
            suppressed["previous_only"] += 1
            continue

        spaces = sorted({
            sources[r.source_id].source_group
            for r in candidate_rows
        })
        publication_hourly = {
            space: [0] * 24
            for space in SPACES
        }
        publication_sources = {}
        published_count = 0
        missing_published_at_count = 0
        outside_window_count = 0

        for row in publication_rows:
            published_at = row.published_at

            if published_at is None:
                missing_published_at_count += 1
                continue

            if not (
                current_start
                <= published_at
                < current_end
            ):
                outside_window_count += 1
                continue

            source = sources[row.source_id]
            space = source.source_group

            bucket = int(
                (
                    published_at - current_start
                ).total_seconds()
                // 3600
            )

            publication_hourly[space][bucket] += 1
            published_count += 1

            source_row = publication_sources.setdefault(
                row.source_id,
                {
                    "source_id": row.source_id,
                    "source_name": source.source_name[:240],
                    "source_type": source.source_type[:40],
                    "source_group": space,
                    "publications": 0,
                    "first_published_at": published_at,
                    "last_published_at": published_at,
                },
            )

            source_row["publications"] += 1
            source_row["first_published_at"] = min(
                source_row["first_published_at"],
                published_at,
            )
            source_row["last_published_at"] = max(
                source_row["last_published_at"],
                published_at,
            )

        publication_source_ranking = sorted(
            publication_sources.values(),
            key=lambda row: (
                -row["publications"],
                row["first_published_at"],
                row["source_name"].casefold(),
                row["source_id"],
            ),
        )

        for row in publication_source_ranking:
            row["first_published_at"] = (
                row["first_published_at"].isoformat()
            )
            row["last_published_at"] = (
                row["last_published_at"].isoformat()
            )

        publication_spread = {
            "window_start": current_start.isoformat(),
            "window_end": current_end.isoformat(),
            "published_count": published_count,
            "missing_published_at_count": (
                missing_published_at_count
            ),
            "outside_window_count": outside_window_count,
            "hourly": publication_hourly,
            "source_ranking": publication_source_ranking,
        }

        dynamics = {}
        for space in SPACES:
            metrics = {}
            for name, (start, end) in bounds.items():
                subset = [r for r in rows if sources[r.source_id].source_group == space and start <= r.collected_at < end]
                denominator = coverage[name][space]["occurrence_count"]
                metrics[name] = {**counts(subset), "observed_flow_count": denominator, "share": len(subset) / denominator if denominator else None}
            old, new = metrics["previous"]["occurrence_count"], metrics["current"]["occurrence_count"]
            old_share, new_share = metrics["previous"]["share"], metrics["current"]["share"]
            dynamics[space] = {
                **metrics,
                "delta": new - old,
                "change_status": "new_in_observed_window" if old == 0 and new > 0 else "not_observed" if old == new == 0 else "observed_in_previous_window",
                "growth_ratio": new / old if old else None,
                "share_delta": new_share - old_share if old_share is not None and new_share is not None else None,
            }
        distances = []
        for cid in group:
            value = distance(cid, representative)
            distances.append({"content_id": cid, "distance": value, "distance_band": config.band(value) if value is not None else "unavailable"})
        # Поле використовується лише для пошуку в UI; не дублюємо
        # весь великий кластер у presentation snapshot.
        search_text = " ".join(
            dict.fromkeys(
                contents[cid].title.strip()
                for cid in group
                if contents[cid].title.strip()
            )
        )[:2000]

        candidates.append({
            "candidate_id": representative,
            "representative_title": contents[representative].title[:240],
            "search_text": search_text,
            "last_observed": last_observed_rows[-1].collected_at.isoformat(),
            "first_published_at": first_published_at,
            "last_published_at": last_published_at,
            "representative_content_id": representative,
            "content_ids": group,
            "interpretation_status": "unverified",
            "source_groups": spaces,
            "cross_space": len(spaces) == 2,
            "exact_republication_content_ids": [
                cid
                for cid in group
                if len({
                    r.source_id
                    for r in candidate_rows
                    if r.content_id == cid
                }) >= 2
            ],
            **counts(candidate_rows),
            "distances_to_representative": distances,
            "dynamics": dynamics,
            "publication_spread": publication_spread,
            "chronology": [{"occurrence_id": r.occurrence_id, "content_id": r.content_id, "source_id": r.source_id, "source_group": sources[r.source_id].source_group, "source_name": sources[r.source_id].source_name[:240], "source_type": sources[r.source_id].source_type[:40], "title": contents[r.content_id].title[:240], "external_ref": r.external_ref[:2048], "collected_at": r.collected_at.isoformat(), "published_at": r.published_at.isoformat() if r.published_at else None} for r in chronology_rows[:config.max_evidence]],
            "evidence_omitted_count": max(0, len(candidate_rows) - config.max_evidence),
            "evidence_references": [{"content_id": cid, "text": contents[cid].text[:config.evidence_chars], "text_truncated": len(contents[cid].text) > config.evidence_chars} for cid in group[:config.max_evidence]],
        })
    # Signals — це насамперед поширення між джерелами.
    # Свіжість уже обмежена часовим вікном, тому вона не повинна
    # витісняти широку ампліфікацію лише через різницю в секунди.
    # Обсяг content/occurrences сам по собі не є пріоритетом.
    candidates.sort(key=lambda c: (
        -c['source_count'],
        -c['cross_space'],
        -len(c['exact_republication_content_ids']),
        -datetime.fromisoformat(c['last_observed']).timestamp(),
        -c['content_count'],
        c['candidate_id'],
    ))
    eligible_count = len(candidates)
    candidates = candidates[:config.display_limit]
    selected_set = set(selected)
    related = [{"left_content_id": a, "right_content_id": b, "distance": d,
                "interpretation_status": "unverified"}
               for (a, b), d in pair_distances.items()
               if a in selected_set
               and b in selected_set
               and final_membership.get(a) != final_membership.get(b)
               and config.max_distance < d <= config.related_distance]
    related.sort(key=lambda row: (row['distance'], row['left_content_id'], row['right_content_id']))
    related_available = len(related)
    related = related[:config.max_related_links]
    presentation = dict(selected_content_ids=sorted(selected), core_group_count=len(groups), eligible_candidate_count=eligible_count,
        displayed_candidate_count=len(candidates), display_limit=config.display_limit,
        display_limit_reached=eligible_count > len(candidates), suppressed=suppressed,
        related_links_available=related_available, related_link_count=len(related),
        related_limit_reached=related_available > len(related))
    if config.merge_distance is not None:
        presentation["strict_core_group_count"] = len(strict_groups)
    truncated = truncated or presentation['display_limit_reached'] or presentation['related_limit_reached']
    missing = any(contents[cid].embedding is None and not contents[cid].has_embedding for cid in eligible_ids)
    missing = missing or any(coverage[name][space]["missing_embedding_content_count"] for name in bounds for space in SPACES)
    excluded = any(coverage[name]["excluded"]["occurrence_count"] for name in bounds)
    routing_missing = any(routing_coverage[w][g]["missing"] for w in bounds for g in SPACES)
    warnings = []
    if routing_missing:
        warnings.append("Матеріали без routing decision виключено; routing coverage неповне.")
    if truncated:
        warnings.append("Вибірку усічено; coverage описує лише наданий спостережуваний потік.")
    if missing:
        warnings.append("Частина матеріалів не має embeddings; семантичний пошук їх не охоплює.")
    if excluded:
        warnings.append("Невідомі інформаційні простори виключено з кандидатів.")
    if any(coverage[name][space]["missing_published_at"] for name in bounds for space in (*SPACES, "excluded")):
        warnings.append("Для частини публікацій час публікації невідомий.")
    return {"critical_incomplete": data.critical_incomplete, "selection": data.selection, "coverage": coverage, "routing_coverage": routing_coverage, "presentation": presentation, "related_links": related, "outside_window_occurrence_count": outside, "omitted_content_count": len(eligible_ids) - len(selected), "truncated": truncated, "incomplete": truncated or missing or excluded or routing_missing, "candidates": candidates, "warnings": warnings}
