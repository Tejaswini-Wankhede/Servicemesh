-- One database per organization.
--
-- This is the schema-level expression of the project's central premise: the
-- marketplace, OEM, warranty provider, service centre and parts supplier are
-- independently governed systems. Giving them separate databases means no
-- single SQL transaction can span them, which is precisely why ServiceMesh
-- needs a Saga-style coordination layer with idempotency and compensation
-- rather than a distributed two-phase commit.
--
-- The `servicemesh` database itself is created by POSTGRES_DB.

CREATE DATABASE org_marketplace;
CREATE DATABASE org_manufacturer;
CREATE DATABASE org_warranty;
CREATE DATABASE org_service_centre;
CREATE DATABASE org_parts_supplier;
