-- ServiceMesh 0002: persisted facts surfaced by provider portals.
-- For a fresh deployment create_all includes these columns. For an existing
-- PostgreSQL database run these ALTER statements once before upgrading.
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS assigned_repair_person VARCHAR(128);
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS diagnosis_notes TEXT;
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS repair_notes TEXT;
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS verification_result VARCHAR(32);
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS part_delivery_status VARCHAR(32);
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS part_eta VARCHAR(64);
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS part_price DOUBLE PRECISION;
ALTER TABLE service_transactions ADD COLUMN IF NOT EXISTS supplier_decision VARCHAR(32);
