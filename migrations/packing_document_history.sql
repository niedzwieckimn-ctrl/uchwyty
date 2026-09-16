CREATE TABLE IF NOT EXISTS fulfillment_document_history(
    order_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    document_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    file_hash TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(order_id,kind,document_id)
);

CREATE INDEX IF NOT EXISTS idx_fulfillment_document_history_document
    ON fulfillment_document_history(kind,document_id,order_id);

INSERT OR IGNORE INTO fulfillment_document_history(
    order_id,kind,document_id,content_hash,path,created_at,file_hash
)
SELECT order_id,kind,document_id,content_hash,path,created_at,COALESCE(file_hash,'')
FROM fulfillment_documents
WHERE kind='packing_list';

CREATE TRIGGER IF NOT EXISTS fulfillment_document_history_no_update
BEFORE UPDATE ON fulfillment_document_history
BEGIN SELECT RAISE(ABORT, 'fulfillment document history is immutable'); END;

CREATE TRIGGER IF NOT EXISTS fulfillment_document_history_no_delete
BEFORE DELETE ON fulfillment_document_history
BEGIN SELECT RAISE(ABORT, 'fulfillment document history is immutable'); END;
