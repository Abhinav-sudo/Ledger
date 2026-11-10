CREATE TABLE vendors (
    id         text PRIMARY KEY,
    name       text NOT NULL,
    short_name text NOT NULL,
    email      text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE vendor_bank_accounts (
    id         bigserial PRIMARY KEY,
    vendor_id  text NOT NULL REFERENCES vendors (id),
    account    text NOT NULL,
    valid_from date NOT NULL,
    valid_to   date,
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- A vendor has at most one account in force; a change closes the old row and opens a new one.
CREATE UNIQUE INDEX vendor_bank_accounts_one_open
    ON vendor_bank_accounts (vendor_id) WHERE valid_to IS NULL;

CREATE TABLE purchase_orders (
    po_number text PRIMARY KEY,
    vendor_id text NOT NULL REFERENCES vendors (id),
    status    text NOT NULL CHECK (status IN ('open', 'closed')),
    currency  char(3) NOT NULL,
    issued_on date NOT NULL
);

CREATE TABLE po_lines (
    po_number   text NOT NULL REFERENCES purchase_orders (po_number),
    line_no     int NOT NULL,
    sku         text NOT NULL,
    description text NOT NULL,
    qty         numeric(12, 3) NOT NULL CHECK (qty > 0),
    unit_price  numeric(14, 2) NOT NULL CHECK (unit_price >= 0),
    PRIMARY KEY (po_number, line_no)
);

CREATE TABLE goods_receipts (
    id           bigserial PRIMARY KEY,
    po_number    text NOT NULL,
    line_no      int NOT NULL,
    qty_received numeric(12, 3) NOT NULL,
    received_on  date NOT NULL,
    FOREIGN KEY (po_number, line_no) REFERENCES po_lines (po_number, line_no)
);

CREATE TABLE invoices (
    id               text PRIMARY KEY,
    vendor_id        text NOT NULL REFERENCES vendors (id),
    invoice_no       text NOT NULL,
    invoice_date     date NOT NULL,
    -- Deliberately not a foreign key: invoices can cite POs that do not exist.
    po_number        text,
    currency         char(3) NOT NULL,
    remit_to_account text NOT NULL,
    total            numeric(14, 2) NOT NULL,
    status           text NOT NULL DEFAULT 'received',
    pdf_path         text,
    received_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (vendor_id, invoice_no)
);

CREATE TABLE invoice_lines (
    invoice_id  text NOT NULL REFERENCES invoices (id),
    line_no     int NOT NULL,
    sku         text NOT NULL,
    description text NOT NULL,
    qty         numeric(12, 3) NOT NULL,
    unit_price  numeric(14, 2) NOT NULL,
    po_line     int,
    PRIMARY KEY (invoice_id, line_no)
);

CREATE TABLE payment_intents (
    id              uuid PRIMARY KEY,
    invoice_id      text NOT NULL REFERENCES invoices (id),
    amount          numeric(14, 2) NOT NULL CHECK (amount > 0),
    vendor_account  text NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    status          text NOT NULL CHECK (status IN ('pending', 'sent', 'confirmed', 'failed')),
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error      text,
    bank_payment_id text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    confirmed_at    timestamptz,
    -- One intent per invoice: a re-run that computes a different amount or account must not
    -- create a second payment.
    UNIQUE (invoice_id)
);

CREATE INDEX payment_intents_due ON payment_intents (next_attempt_at) WHERE status = 'pending';

CREATE TABLE graph_jobs (
    id          bigserial PRIMARY KEY,
    invoice_id  text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('run', 'resume')),
    payload     jsonb NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done')),
    lease_until timestamptz,
    attempts    int NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (invoice_id, kind)
);

CREATE INDEX graph_jobs_queued ON graph_jobs (id) WHERE status = 'queued';

CREATE TABLE audit_log (
    id         bigserial PRIMARY KEY,
    invoice_id text NOT NULL,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL,
    event      text NOT NULL,
    detail     jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX audit_log_invoice ON audit_log (invoice_id, at);
