-- CoreMind World Model — SurrealDB schema (L2)
-- Apply with: surreal import --conn ... --user ... --pass ... --ns coremind --db world schema.surql
-- All DEFINE statements use IF NOT EXISTS — safe to re-apply on every daemon start.

-- ---------------------------------------------------------------------------
-- Entity table
-- ---------------------------------------------------------------------------
DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS type             ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS display_name     ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS created_at       ON entity TYPE datetime;
DEFINE FIELD IF NOT EXISTS updated_at       ON entity TYPE datetime;
DEFINE FIELD IF NOT EXISTS properties       ON entity TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS source_plugins   ON entity TYPE array<string> DEFAULT [];
DEFINE INDEX IF NOT EXISTS entity_type_id   ON entity FIELDS type, id UNIQUE;

-- ---------------------------------------------------------------------------
-- Event table
-- ---------------------------------------------------------------------------
DEFINE TABLE IF NOT EXISTS event SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS timestamp        ON event TYPE datetime;
DEFINE FIELD IF NOT EXISTS source           ON event TYPE string;
DEFINE FIELD IF NOT EXISTS source_version   ON event TYPE string;
DEFINE FIELD IF NOT EXISTS entity           ON event TYPE record<entity>;
DEFINE FIELD IF NOT EXISTS attribute        ON event TYPE string;
DEFINE FIELD IF NOT EXISTS value            ON event TYPE any;
DEFINE FIELD IF NOT EXISTS confidence       ON event TYPE float;
DEFINE FIELD IF NOT EXISTS signature        ON event TYPE option<string>;
DEFINE FIELD IF NOT EXISTS unit             ON event TYPE option<string>;
DEFINE INDEX IF NOT EXISTS event_entity_attr_time ON event FIELDS entity, attribute, timestamp;

-- ---------------------------------------------------------------------------
-- Relationship table
-- ---------------------------------------------------------------------------
DEFINE TABLE IF NOT EXISTS relationship SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS type             ON relationship TYPE string;
DEFINE FIELD IF NOT EXISTS from             ON relationship TYPE record<entity>;
DEFINE FIELD IF NOT EXISTS to               ON relationship TYPE record<entity>;
DEFINE FIELD IF NOT EXISTS weight           ON relationship TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS created_at       ON relationship TYPE datetime;
DEFINE FIELD IF NOT EXISTS last_reinforced  ON relationship TYPE datetime;

-- ---------------------------------------------------------------------------
-- Autonomy change audit table
-- ---------------------------------------------------------------------------
DEFINE TABLE IF NOT EXISTS autonomy_change SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS domain           ON autonomy_change TYPE string;
DEFINE FIELD IF NOT EXISTS old_slider       ON autonomy_change TYPE float;
DEFINE FIELD IF NOT EXISTS new_slider       ON autonomy_change TYPE float;
DEFINE FIELD IF NOT EXISTS reason           ON autonomy_change TYPE string;
DEFINE FIELD IF NOT EXISTS changed_by       ON autonomy_change TYPE string;
DEFINE FIELD IF NOT EXISTS changed_at       ON autonomy_change TYPE datetime;
DEFINE INDEX IF NOT EXISTS autonomy_change_domain_time ON autonomy_change FIELDS domain, changed_at;
