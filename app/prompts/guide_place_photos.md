# Pick destination photos for a Korean itinerary page

A hotel-centred itinerary page shows each outside sight with one photo. For
every PLACE you receive the moment it illustrates and up to six freely licensed
photos from Wikimedia Commons (title, description and, when known, the date it
was taken), and the TRIP's month and season.

Pick the photo a traveller would recognise as that place: the sight itself, as
a visitor sees it (the fountain, the façade, the garden, the street). Reject a
photo of another place with the same name (another city's Pantheon), a detail
(a fresco, a statue's head, a ceiling) when a view of the whole sight exists, a
close-up of people, and anything that is not a photograph. Prefer a photo
that looks like the trip's season: one taken in or near the trip's month, or
one with nothing seasonal in it. Avoid one that clearly shows another season
(summer crowds in shorts, deep summer green, bright summer flowers for a winter
trip; snow where the trip's season rarely has it). Pick none (-1) when
no candidate clearly shows the place.

Return JSON only:

{"picks": [{"place": "", "pick": 0}]}

`pick` is the candidate's index, or -1.
