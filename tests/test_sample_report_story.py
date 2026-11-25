"""The example report sells the ghostwriter's work without overstating it.

The client-facing summary and goal line carry no disclaimers; those live in
"About these numbers" at the end of every report. The example's top posts
are original posts, each with its own text, as a real report would show.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.demo.sample_data import SAMPLE


TEMPLATES = Environment(
    loader=FileSystemLoader(
        str(Path(__file__).resolve().parents[1] / "app" / "templates")
    )
)


def render(snapshot):
    return TEMPLATES.get_template("report_body.html").render(snapshot=snapshot)


def test_sample_summary_reports_delivery_without_disclaimers():
    summary = SAMPLE["snapshot"]["writing"]["executive_summary"]

    assert "delivered everything planned" in summary
    assert "not monthly account traffic" not in summary
    assert "proof of business growth" not in summary


def test_target_note_moves_to_about_these_numbers():
    html = render(SAMPLE["snapshot"])
    about = html.split("About these numbers", 1)[1]
    goals = html.split("Goals and previous reports", 1)[1].split(
        "About these numbers", 1
    )[0]

    assert "not verified\n            contractual results" in about
    assert "not a verified contractual result" not in goals
    assert "not account-wide monthly traffic" in about


def test_sample_top_posts_are_distinct_original_posts():
    top = SAMPLE["snapshot"]["top_posts"]
    texts = [post["post_text"] for post in top]

    assert len(top) == 6
    assert len(set(texts)) == len(texts)
    assert all(post["content_type"] == "Post" for post in top)
    assert not any(text.startswith("@") for text in texts)


def test_sample_replies_reach_fewer_people_than_posts():
    groups = {
        (group["authorship"], group["content_type"]): group
        for group in SAMPLE["snapshot"]["breakdown"]["authorship_content_type"]
    }

    assert (
        groups[("Ghostwriter", "Post")]["impressions"]["median"]
        > groups[("Ghostwriter", "Reply")]["impressions"]["median"]
    )


def test_sample_median_clears_its_target():
    goal = SAMPLE["snapshot"]["goal"]

    assert (
        goal["observed_median_post_impressions"]
        > goal["target_post_impressions"]
    )
