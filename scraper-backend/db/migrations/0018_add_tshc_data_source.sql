-- Add TSHC_WEBSITE to data_source_enum for Telangana High Court adapter

ALTER TYPE data_source_enum ADD VALUE IF NOT EXISTS 'TSHC_WEBSITE';
