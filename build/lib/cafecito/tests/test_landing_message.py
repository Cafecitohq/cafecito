from cafecito import engine as landing


def test_clean_message():
    msg = landing._land_message("my feature", "cs_abc123")
    assert msg == (
        "land: my feature\n"
        "\n"
        "Changeset-Id: cs_abc123\n"
        "Signed-off-by: cafecito-engine <engine@cafecito.local>"
    )


def test_regenerated_message():
    msg = landing._land_message("my feature", "cs_abc123", regenerated=True)
    assert msg == (
        "land: my feature\n"
        "\n"
        "Changeset-Id: cs_abc123\n"
        "Regenerated: true\n"
        "Signed-off-by: cafecito-engine <engine@cafecito.local>"
    )


def test_title_truncated_to_70():
    long_title = "x" * 80
    msg = landing._land_message(long_title, "cs_x")
    first_line = msg.splitlines()[0]
    assert first_line == "land: " + "x" * 70


def test_regenerated_false_omits_line():
    msg = landing._land_message("t", "cs_1", regenerated=False)
    assert "Regenerated" not in msg
