import pytest

from bookerbud.normalize.names import PlayerMatcher, normalize_name, player_key


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("LeBron James", "lebron james"),
        ("Nikola Jokić", "nikola jokic"),
        ("Jazz Chisholm Jr.", "jazz chisholm"),
        ("Robert Hassell III", "robert hassell"),
        ("C.J. Abrams", "cj abrams"),
        ("CJ Abrams", "cj abrams"),
        ("De'Aaron Fox", "deaaron fox"),
        ("Karl-Anthony Towns", "karl anthony towns"),
        ("  Shai   Gilgeous-Alexander ", "shai gilgeous alexander"),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_player_key_is_sport_scoped():
    assert player_key("nba", "Nikola Jokić") == "nba:nikola_jokic"
    assert player_key("mlb", "Jazz Chisholm Jr.") == "mlb:jazz_chisholm"


ROSTER = ["Nikola Jokić", "Jazz Chisholm Jr.", "Josh Bell", "Michael Porter Jr.", "Jalen Williams", "Jaylin Williams"]


@pytest.mark.parametrize(
    "book_spelling, expected",
    [
        ("Nikola Jokic", "Nikola Jokić"),  # accents
        ("JAZZ CHISHOLM", "Jazz Chisholm Jr."),  # case + suffix
        ("Joshua Bell", "Josh Bell"),  # nickname table
        ("Mike Porter", "Michael Porter Jr."),  # nickname table + suffix stripped
        ("Micheal Porter", "Michael Porter Jr."),  # typo, same last name, high similarity
        ("Jalen Williams", "Jalen Williams"),
        ("J. Williams", None),  # two Williams on roster -> refuse to guess
        ("Totally Unknown", None),
    ],
)
def test_player_matcher(book_spelling, expected):
    assert PlayerMatcher(ROSTER).match(book_spelling) == expected


def test_ambiguous_roster_keys_detected():
    m = PlayerMatcher(["Josh Smith", "Josh Smith Jr.", "Bob Jones"])
    assert m.ambiguous_keys() == {"josh smith"}
