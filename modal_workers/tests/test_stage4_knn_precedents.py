from __future__ import annotations


def test_similar_resolved_cases_orders_by_knn_distance():
    from modal_workers.shared.compute import similar_resolved_cases

    class StubSb:
        def _rest(self, method, path, *, params=None):
            assert method == "GET"
            assert path == "eval_harness"
            return [
                {
                    "id": "eval-low",
                    "asset_id": "asset-low",
                    "reference_assessment_date": "2025-01-01",
                    "realized_outcome": "miss",
                    "realized_outcome_data": {},
                    "notes": None,
                    "fda_assets": {
                        "reference_class_signature": "other_class",
                        "ticker": "LOW",
                        "drug_name": "Low",
                    },
                },
                {
                    "id": "eval-high",
                    "asset_id": "asset-high",
                    "reference_assessment_date": "2024-01-01",
                    "realized_outcome": "hit",
                    "realized_outcome_data": {"realized_move_pct": 28.0},
                    "notes": None,
                    "fda_assets": {
                        "reference_class_signature": "phase3_psych_NDA",
                        "ticker": "HIGH",
                        "drug_name": "High",
                        "indication_normalized": "psych",
                        "program_status": "phase3",
                        "application_type": "NDA",
                    },
                },
            ]

    cases = similar_resolved_cases(
        StubSb(),
        "phase3_psych_NDA",
        k=2,
        exclude_asset_id="current",
    )

    assert cases[0].eval_harness_id == "eval-high"
    assert cases[0].similarity_score is not None
    assert "reference_class" in cases[0].match_reasons
