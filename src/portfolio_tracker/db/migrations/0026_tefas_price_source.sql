-- Turkish funds back to TEFAS.
--
-- fintables started answering every request with a Cloudflare challenge on
-- 2026-09-15, which left both Turkish funds unpriced. TEFAS, the official
-- register it republished, has a working JSON price endpoint again on its
-- rebuilt site (see TefasProvider). 'tefas' is already a permitted
-- price_source, so this is configuration only.
UPDATE instrument
SET price_source = 'tefas'
WHERE price_source = 'fintables';
