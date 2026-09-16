"""Shared analytical scope for C1 object assignments."""

from __future__ import annotations


def c1_analytical_gate_sql(alias: str) -> str:
    """Return the C1 analytical gate for a trusted SQL table alias."""
    if alias not in {"a", "other"}:
        raise ValueError(f"Unsupported assignment alias: {alias}")

    return f"""
                  AND (
                        {alias}.status = 'confirmed'
                        OR (
                            {alias}.status = 'candidate'
                            AND (
                                {alias}.object_id <> 1
                                OR EXISTS (
                                    SELECT 1
                                    FROM contour_reference_entries gate_ref
                                    WHERE gate_ref.reference_id = {alias}.reference_id
                                      AND gate_ref.verification_status = 'candidate'
                                      AND gate_ref.source_note = 'c1_recall_pack_v1'
                                )
                            )
                        )
                  )
"""
