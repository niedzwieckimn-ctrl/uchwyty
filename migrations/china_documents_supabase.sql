-- Apply before enabling document upload on an instance backed by Supabase.
-- The private PDF bytes are stored in the existing private Storage bucket;
-- this table keeps only the shipment link and storage reference.
CREATE TABLE IF NOT EXISTS public.china_documents (
    id bigint PRIMARY KEY,
    package_id bigint NOT NULL REFERENCES public.china_packages(id),
    original_name text NOT NULL,
    document_type text NOT NULL DEFAULT 'order'
        CHECK (document_type IN ('invoice', 'zc429', 'order')),
    stored_path text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    created_at text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_china_documents_package
    ON public.china_documents(package_id, id);
ALTER TABLE public.china_documents ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.china_documents FROM anon, authenticated;
