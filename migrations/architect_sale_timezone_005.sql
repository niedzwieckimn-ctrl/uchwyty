-- Existing installations: use the Polish business date for retail-sale evidence.
-- The B2B form defaults to the distributor's current date in Poland. Without
-- this setting, submissions between 00:00 and 01:00/02:00 Polish time are
-- rejected as future dates by a database session running in UTC.
alter function public.architect_record_verified_sale(
  bigint, bigint, bigint, integer, date, text, numeric, numeric, text
) set timezone to 'Europe/Warsaw';
