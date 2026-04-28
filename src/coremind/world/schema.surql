-- CoreMind World Model — SurrealDB schema (L2)
-- Apply with: surreal import --conn ... --user ... --pass ... --ns coremind --db world schema.surql

-- ---------------------------------------------------------------------------
-- Entity table
-- ---------------------------------------------------------------------------
DEFINE TABLE entity SCHEMAFULL;
DEFINE FIELD type             ON entity TYPE string;
DEFINE FIELD display_name     ON entity TYPE string;
DEFINE FIELD created_at       ON entity TYPE datetime;
DEFINE FIELD updated_at       ON entity TYPE datetime;
DEFINE FIELD properties       ON entity TYPE object FLEXIBLE DEFAULT {};
DEFINE FIELD source_plugins   ON entity TYPE array<string> DEFAULT [];
DEFINE INDEX entity_type_id   ON entity FIELDS type, id UNIQUE;

-- ---------------------------------------------------------------------------
-- Event table
-- ---------------------------------------------------------------------------
DEFINE TABLE event SCHEMAFULL;
DEFINE FIELD timestamp        ON event TYPE datetime;
DEFINE FIELD source           ON event TYPE string;
DEFINE FIELD source_version   ON event TYPE string;
DEFINE FIELD entity           ON event TYPE record<entity>;
DEFINE FIELD attribute        ON event TYPE string;
DEFINE FIELD value            ON event TYPE any;
DEFINE FIELD confidence       ON event TYPE float;
DEFINE FIELD signature        ON event TYPE option<string>;
DEFINE FIELD unit             ON event TYPE option<string>;
DEFINE INDEX event_entity_attr_time ON event FIELDS entity, attribute, timestamp;

-- ---------------------------------------------------------------------------
-- Relationship table
-- ---------------------------------------------------------------------------
DEFINE TABLE relationship SCHEMAFULL;
DEFINE FIELD type             ON relationship TYPE string;
DEFINE FIELD from             ON relationship TYPE record<entity>;
DEFINE FIELD to               ON relationship TYPE record<entity>;
DEFINE FIELD weight           ON relationship TYPE float DEFAULT 1.0;
DEFINE FIELD created_at       ON relationship TYPE datetime;
DEFINE FIELD last_reinforced  ON relationship TYPE datetime;
