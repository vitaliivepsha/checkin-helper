import venue_scrape

PAGE = """
<div id="main-stream">
 <div class="item " id="checkin_200" data-checkin-id="200"><div class=""><div class="checkin"><div class="top">
  <a href="/b/mikkeller-peach-treaty/6894992" class="label"><img alt="x"></a>
  <p class="text"><a href="/user/Nilsrn" class="user">Nils</a> is drinking a
   <a href="/b/mikkeller-peach-treaty/6894992">Peach Treaty</a> by <a href="/mikkellerbeer">Mikkeller</a>
   at <a href="/v/mikkeller-cph-airport/7795784">Mikkeller CPH Airport</a></p></div></div></div></div>
 <div class="item " id="checkin_199" data-checkin-id="199"><div class=""><div class="checkin"><div class="top">
  <p class="text"><a href="/user/anna_b" class="user">Anna</a> is drinking an
   <a href="/b/x-brew-ipa/123">Caf&eacute; &amp; Co IPA</a> by <a href="/xbrew">X &amp; Y Brewing</a></p></div></div></div></div>
 <div class="item " id="checkin_198" data-checkin-id="198"><div class="checkin"><div class="top">
  <p class="text">no beer link here</p></div></div></div>
</div>
"""


def test_parse_activity_extracts_fields():
    items = venue_scrape.parse_activity(PAGE)
    assert [it["checkin_id"] for it in items] == [200, 199]  # odd entry skipped, order kept
    first = items[0]
    assert first["user"]["user_name"] == "Nilsrn"
    assert first["beer"] == {"bid": 6894992, "beer_name": "Peach Treaty"}
    assert first["brewery"] == {"brewery_id": None, "brewery_name": "Mikkeller"}
    assert first["venue"] == {"venue_id": 7795784, "venue_name": "Mikkeller CPH Airport"}


def test_parse_activity_unescapes_and_tolerates_missing_venue():
    second = venue_scrape.parse_activity(PAGE)[1]
    assert second["beer"]["beer_name"] == "Café & Co IPA"
    assert second["brewery"]["brewery_name"] == "X & Y Brewing"
    assert second["venue"] == {"venue_id": None, "venue_name": None}


def test_parse_activity_empty_page():
    assert venue_scrape.parse_activity("<html></html>") == []
