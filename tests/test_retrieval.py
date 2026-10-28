from app.documents import _cross_language_overlap, _search_tokens


def test_hindi_transliterated_menu_name_matches_english_chunk():
    query = _search_tokens("क्लासिक वेज टिफिन में क्या क्या आता है")
    content = _search_tokens("Classic Veg Tiffin: Seasonal vegetable, dal, 4 rotis, rice, and salad. INR 120")
    assert _cross_language_overlap(query, content) >= 0.60


def test_hindi_transliterated_mini_lunch_box_matches_english_chunk():
    query = _search_tokens("मिनी लंच बॉक्स में क्या आता है")
    content = _search_tokens("Mini Lunch Box: One vegetable, 2 rotis, and a small rice portion. INR 85")
    assert _cross_language_overlap(query, content) >= 0.60
