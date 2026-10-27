# gb-open-streets

Per-council extracts of open address-point, road and place-name data for Great
Britain (England, Scotland, Wales), rebuilt quarterly. One gzipped JSON file per
local authority district, named by its ONS code (e.g. `S12000006.json.gz`), plus
`index.json` listing every council with its name and size.

Latest files: `https://github.com/standayweb/gb-open-streets/releases/latest/download/<file>`

Each file holds:
- address points (position, postcode, ward) from the ONS UPRN Directory,
- road centre lines with names or road numbers from OS Open Roads,
- populated places from OS Open Names.

No personal data. An address point is a location and a postcode only.

## Licence and attribution

Contains OS data © Crown copyright and database right. Contains National
Statistics data © Crown copyright and database right. Contains Royal Mail data
© Royal Mail copyright and database right. All under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
