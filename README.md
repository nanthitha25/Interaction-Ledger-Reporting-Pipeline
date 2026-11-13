# Interaction Ledger & Reporting Pipeline

Production-grade, idempotent data pipeline ingesting multi-workflow lead batches (CSV) and telephonic interaction event streams (JSON Lines) into an append-only PostgreSQL ledger with zero plaintext PII at rest.

---

## Architecture Overview

```
                                      [ Drop Input: CSV & JSONL ]
                                                  │
                                    ┌─────────────┴─────────────┐
                                    ▼                           ▼
                           [ Lead CSV Ingestion ]     [ Event Stream Ingestion ]
                                    │                           │
                    ┌───────────────┴───────────────┐           │
                    ▼                               ▼           │
          [ Header Normalization ]        [ Schema Validation ] │
                    │                               │           │
                    └───────────────┬───────────────┘           │
                                    ▼                           │
                        [ Intra-Batch Deduplication ]           │
                                    │                           │
                                    ▼                           ▼
                        [ PII Vault Cryptographic Engine (AES-256-GCM + HMAC Blind Index) ]
                                    │                           │
                                    ▼                           ▼
                           [ Lead Repository ]       [ Append-Only Ledger ]
                                    │                           │
                                    └─────────────┬─────────────┘
                                                  ▼
                                       [ PostgreSQL 15+ DB ]
                                       - uq_leads_identity
                                       - trg_ledger_immutability
                                       - trg_ledger_prevent_delete
                                       - trg_ledger_prevent_truncate
```

### Core Components

1. **Schema-Driven Lead Validation & Normalization (`src.ingestion`)**
   - Header alias resolution with whitespace/casing normalization.
   - Day-first date parsing (`YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `DD-Mon-YYYY`).
   - Currency & decimal cleaning (`₹`, `Rs.`, `INR`, commas stripped; rounded to 2 decimal places).
   - Canonical Indian phone normalization (`+91XXXXXXXXXX`).
   - Rejections logging in schema-order (`lead_rejections` table).

2. **Deduplication Engine (`src.ingestion.deduplication`)**
   - Identity: `(workflow_id, canonical_phone)`.
   - Permanent `batch_id` locked to the first valid occurrence.
   - Last-non-null-write-wins merging for subsequent occurrences.
   - Schema defaults applied post-merge only if field was never explicitly provided.

3. **Cryptographic Engine & PII Vault (`src.security`)**
   - **Zero Plaintext PII at Rest:** All phone numbers and customer names encrypted with AES-256-GCM authenticated encryption (`v1:<base64(nonce + ct + tag)>`).
   - **Blind Indexing:** HMAC-SHA256 64-character hex digests for $O(1)$ equality joins without exposing plaintext phone numbers.
   - **Key Separation:** Enforces separate cryptographic keys for HMAC indexing and AES encryption.
   - **Payload Scrubbing:** Recursive scrubbing of sensitive PII keys from raw event JSON payloads before ledger persistence.

4. **PostgreSQL Lead Repository (`src.repository.lead_repository`)**
   - Single-query atomic upsert:
     ```sql
     INSERT INTO leads (...) VALUES (...)
     ON CONFLICT (workflow_id, phone_bidx) DO UPDATE SET
         phone_encrypted = EXCLUDED.phone_encrypted,
         customer_name_encrypted = COALESCE(EXCLUDED.customer_name_encrypted, leads.customer_name_encrypted),
         external_ref = COALESCE(EXCLUDED.external_ref, leads.external_ref),
         policy_expiry = COALESCE(EXCLUDED.policy_expiry, leads.policy_expiry),
         premium_amount = COALESCE(EXCLUDED.premium_amount, leads.premium_amount),
         order_date = COALESCE(EXCLUDED.order_date, leads.order_date),
         product = COALESCE(EXCLUDED.product, leads.product),
         preferred_language = COALESCE(%(explicit_preferred_language)s, leads.preferred_language),
         updated_at = CURRENT_TIMESTAMP
     RETURNING ..., (xmax = 0) AS is_inserted;
     ```
   - Transactional isolation with zero partial writes on failure.

5. **Database Infrastructure & Migrations (`src.db`)**
   - Thread-safe `ThreadedConnectionPool` with automatic transaction management.
   - Versioned transactional migration runner (`001_initial_schema.sql`).
   - Append-only immutability triggers (`fn_enforce_ledger_immutability`, `fn_prevent_ledger_delete`, `fn_prevent_ledger_truncate`).

---

## Directory Structure

```
interaction-ledger/
├── schemas/
│   ├── motor_renewal.json          # Outbound motor renewal lead schema
│   ├── policy_feedback.json        # Post-purchase device protection feedback schema
│   └── dispositions.csv            # Disposition catalogue (terminal / success codes)
├── specs/                          # Architecture & design specifications
│   ├── 00-project-overview.md
│   ├── 01-functional-requirements.md
│   ├── 02-data-ingestion.md
│   ├── 03-requirements-analysis.md
│   ├── 04-system-architecture.md
│   └── 05-database-design.md
├── src/
│   ├── config.py                   # Environment & cryptographic config validation
│   ├── db/
│   │   ├── connection.py           # Thread-safe pool & transaction manager
│   │   └── migrations/
│   │       ├── 001_initial_schema.sql
│   │       ├── 001_initial_schema.rollback.sql
│   │       └── runner.py           # Transactional migration runner
│   ├── ingestion/
│   │   ├── headers.py              # Header normalization & alias mapping
│   │   ├── row_reader.py           # Non-blank row extraction & tracking
│   │   ├── normalizers.py          # Phone, date, decimal, and enum normalizers
│   │   ├── validator.py            # Schema-order row validation & rejections
│   │   ├── deduplication.py        # Lead deduplication & merging engine
│   │   └── lead_pipeline.py        # End-to-end lead ingestion pipeline
│   ├── repository/
│   │   └── lead_repository.py      # PostgreSQL LeadRepository implementation
│   └── security/
│       ├── crypto.py               # AES-256-GCM AEAD & HMAC-SHA256 indexing
│       └── vault.py                # PII Vault abstraction & payload scrubber
└── tests/                          # 337 unit, integration, and security tests
```

---

## Getting Started

### 1. Prerequisites
- Python 3.10+ (tested with Python 3.14)
- PostgreSQL 15+

### 2. Environment Configuration
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Ensure PostgreSQL connection string and cryptographic keys are configured:
```ini
DATABASE_URL=postgresql://localhost:5432/interaction_ledger_test
DB_MIN_CONNECTIONS=1
DB_MAX_CONNECTIONS=10

HMAC_SECRET_KEY=dev_hmac_secret_key_32_bytes_pad
AES_ENCRYPTION_KEY=dev_aes_encryption_key_32_bytes!
ENVIRONMENT=development
```

### 3. Run Migrations
```bash
python -m src.db.migrations.runner
```

### 4. Run Test Suite
```bash
pytest
```
Executes all 337 tests across unit, security, repository, pipeline, and PostgreSQL integration suites:
```
============================= 337 passed in 1.15s ==============================
```
