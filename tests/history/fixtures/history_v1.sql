-- Frozen schema 1 as released in 0.3.x, with the shapes real histories contain.
CREATE TABLE series (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    last_ep REAL,
    watch_updated_at TEXT,
    download_updated_at TEXT
);
CREATE TABLE downloaded_episode (
    series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    episode REAL NOT NULL,
    recorded_at TEXT,
    PRIMARY KEY (series_id, episode)
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO series VALUES ('1', 'fate', 'Fate', 'https://shinden.pl/series/1-fate', 4.0, '2026-01-01T00:00:00+00:00', '2026-01-02T00:00:00+00:00');
INSERT INTO series VALUES ('2', 'bleach', 'Bleach', 'https://shinden.pl/series/2-bleach', 12.5, '2026-02-01T00:00:00+00:00', NULL);
INSERT INTO series VALUES ('3', 'k-on', 'K-On!', 'https://shinden.pl/series/3-k-on', NULL, NULL, '2026-03-01T00:00:00+00:00');
INSERT INTO series VALUES ('4', 'legacy', '4', '', 0.0, NULL, NULL);
INSERT INTO downloaded_episode VALUES ('1', 1.0, '2026-01-02T00:00:00+00:00');
INSERT INTO downloaded_episode VALUES ('1', 2.0, NULL);
INSERT INTO downloaded_episode VALUES ('3', 0.5, NULL);
INSERT INTO downloaded_episode VALUES ('3', 7.0, '2026-03-01T00:00:00+00:00');
INSERT INTO meta VALUES ('legacy_json_sha', '0123456789abcdef');
INSERT INTO meta VALUES ('legacy_json_status', 'done');
INSERT INTO meta VALUES ('legacy_json_imported', '3');
INSERT INTO meta VALUES ('legacy_json_skipped', '1');
PRAGMA user_version = 1;
