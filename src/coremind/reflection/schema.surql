-- CoreMind Reflection Layer (L7) — SurrealDB schema
-- Apply with: surreal import --conn ... --user ... --pass ... \
--   --ns coremind --db reflection schema.surql
--
-- Two tables:
--   * prediction_evaluation — per-prediction outcomes (Task 4.2)
--   * calibration_diagram   — per (layer, model) reliability diagram (Task 4.3)
--
-- Both are written exclusively by the reflection store adapter, matching the
-- project's "one writer per stream" rule.

-- ---------------------------------------------------------------------------
-- prediction_evaluation
-- ---------------------------------------------------------------------------
DEFINE TABLE prediction_evaluation SCHEMAFULL;
DEFINE FIELD cycle_id              ON prediction_evaluation TYPE string;
DEFINE FIELD prediction_id         ON prediction_evaluation TYPE string;
DEFINE FIELD hypothesis            ON prediction_evaluation TYPE string;
DEFINE FIELD falsifiable_by        ON prediction_evaluation TYPE string;
DEFINE FIELD prediction_timestamp  ON prediction_evaluation TYPE datetime;
DEFINE FIELD horizon_end           ON prediction_evaluation TYPE datetime;
DEFINE FIELD confidence            ON prediction_evaluation TYPE float;
DEFINE FIELD verdict               ON prediction_evaluation TYPE string
    ASSERT $value INSIDE ['correct', 'wrong', 'undetermined'];
DEFINE FIELD rationale             ON prediction_evaluation TYPE string DEFAULT '';
DEFINE FIELD evaluated_at          ON prediction_evaluation TYPE datetime;
DEFINE INDEX prediction_evaluation_pk
    ON prediction_evaluation FIELDS cycle_id, prediction_id UNIQUE;
DEFINE INDEX prediction_evaluation_eval_at
    ON prediction_evaluation FIELDS evaluated_at;

-- ---------------------------------------------------------------------------
-- calibration_diagram
-- ---------------------------------------------------------------------------
DEFINE TABLE calibration_diagram SCHEMAFULL;
DEFINE FIELD layer                   ON calibration_diagram TYPE string;
DEFINE FIELD model                   ON calibration_diagram TYPE string;
DEFINE FIELD buckets                 ON calibration_diagram FLEXIBLE TYPE array;
DEFINE FIELD total_samples           ON calibration_diagram TYPE int DEFAULT 0;
DEFINE FIELD brier_sum_squared_error ON calibration_diagram TYPE float DEFAULT 0.0;
DEFINE FIELD last_evaluated_at       ON calibration_diagram TYPE option<datetime>;
DEFINE FIELD updated_at              ON calibration_diagram TYPE datetime;
DEFINE INDEX calibration_diagram_pk
    ON calibration_diagram FIELDS layer, model UNIQUE;
