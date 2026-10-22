-- Baseline init schema for PostgreSQL labs

-- Table for Experiment 01: Isolation Anomalies & Write Skew (Doctors On-Call)
CREATE TABLE IF NOT EXISTS doctors (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    on_call BOOLEAN NOT NULL DEFAULT TRUE
);

-- Table for Experiment 02: Lock Contention & Hot Row Updates
CREATE TABLE IF NOT EXISTS accounts (
    id SERIAL PRIMARY KEY,
    owner VARCHAR(50) NOT NULL,
    balance NUMERIC(12, 2) NOT NULL DEFAULT 1000.00
);

-- Seed initial data
INSERT INTO doctors (name, on_call) VALUES 
    ('Dr. Alice', TRUE),
    ('Dr. Bob', TRUE);

INSERT INTO accounts (owner, balance) VALUES 
    ('Alice', 1000.00),
    ('Bob', 1000.00);
