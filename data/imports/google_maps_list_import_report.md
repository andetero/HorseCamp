# Google Maps Saved List Import Report

This importer only trusts coordinates entered in the CSV `Note` or `Coordinates` field.
It does not call Google Places and does not fuzzy-match names.
Optional columns supported: `Website`, `Phone`, `Description`, `URL`, `Tags`, `Comment`.

## Horse Layovers

- CSV rows: **0**
- Generated records: **0**
- Skipped rows: **0**
- Output: `data/layovers.json`

## Horse Camps

- CSV rows: **217**
- Generated records: **217**
- Skipped rows: **0**
- Output: `data/private_camps.json`

