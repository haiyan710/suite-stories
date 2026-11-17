# Look at destination photos for a Korean itinerary page

A hotel-centred itinerary page shows each outside sight with one photo. Readers
travel on the TRIP DATES given, and the photo must look like that week there.
For every PLACE you get the moment it illustrates and a few freely licensed
photos from Wikimedia Commons (index, file path, title, date taken). All were
dated in the trip's season, but a date is not enough: late February can already
look like spring.

Open every image and judge what it shows:
- It must be that place, as a visitor sees it (the fountain, the façade, the
  garden, the street), not another place with the same name, a detail when a
  view of the whole sight exists, a painting or a close-up of people.
- It must look like the trip dates there. For a winter week, reject blossom,
  spring flowers, fresh spring leaves, people sunbathing or lying on the grass,
  short sleeves; bare or evergreen trees, coats and low winter light fit. For a
  summer week, reject bare trees, coats and snow. When unsure, reject.

Pick none (-1) when no photo of a place passes: an empty slot is better than
the wrong season.

Return JSON only:

{"picks": [{"place": "", "pick": 0, "why": ""}]}

`pick` is the photo's index for that place, or -1; `why` is a few words on what
in the image decided it.
