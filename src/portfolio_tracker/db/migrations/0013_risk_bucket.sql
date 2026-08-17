-- =====================================================================
-- 0013 -- what an instrument is comparable to.
--
-- The point of a peer scan is "this fund carries the same kind of risk as
-- one of yours and returned more". The second half is measurable from
-- prices. The first half is not: nothing in a price series says that a
-- gold-miners ETF and a broad world tracker are different propositions
-- even when their volatilities happen to coincide for a year.
--
-- So the bucket is a judgement, recorded rather than inferred. `peers.py`
-- derives one from asset class and sector when this is NULL, and that
-- fallback is deliberately crude -- it will put a semiconductor ETF and a
-- world tracker in the same bucket, which is exactly the mistake a stated
-- taxonomy exists to prevent. A seeded value always wins.
--
-- Free text rather than a CHECK constraint, because the taxonomy is a
-- judgement about a changing universe and pinning it in the schema would
-- mean a migration every time a new kind of fund is added. `peers.py`
-- documents the vocabulary it uses; nothing breaks if a value falls
-- outside it, the instrument simply matches only others sharing it.
-- =====================================================================

ALTER TABLE instrument ADD COLUMN risk_bucket TEXT;

CREATE INDEX idx_instrument_bucket ON instrument (risk_bucket);
