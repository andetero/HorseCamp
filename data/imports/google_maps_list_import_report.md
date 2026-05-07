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

- CSV rows: **275**
- Generated records: **270**
- Skipped rows: **5**
- Output: `data/private_camps.json`

### Row notes

- Paria River Ranch: skipped, same name as existing manual record: Paria River Ranch
- Hayes Canyon Campground: skipped, same name as existing manual record: Hayes Canyon Campground
- Little Lusk Trail Lodge: skipped, within 100m of existing manual record: Little Lusk Trail Lodge Campground
- Double M Campground: skipped, same name as existing manual record: Double M Campground
- High Knob Ranch Equestrian Campground: skipped, same name as existing manual record: High Knob Ranch Equestrian Campground

