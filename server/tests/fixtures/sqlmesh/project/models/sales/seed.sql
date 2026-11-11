MODEL (
  name sales.regions,
  kind SEED (path '../seeds/regions.csv'),
  description 'Static region list.',
  columns (code TEXT, label TEXT)
);
