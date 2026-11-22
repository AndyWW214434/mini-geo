-- Real scraper records carry full page dumps (answer_html, response_raw): 1-4 MB each, ~20 GB a
-- day at our cadence. Fields over 100 KB are replaced by their size and SHA-256 before storage
-- (in production they would go to object storage keyed by that hash). Everything an answer is
-- parsed from is kept verbatim. The snapshot row records the original body's size and hash.
alter table raw.snapshots add column original_bytes bigint;
alter table raw.snapshots add column original_sha256 text;
comment on column raw.snapshots.body is
  'Snapshot JSON with string fields over 100 KB replaced by {"_omitted": true, "bytes", "sha256"}';
