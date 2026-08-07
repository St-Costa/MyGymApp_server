"""Server-rendered dashboard routes — no auth (see app/gui.py's module
docstring for why), so these tests just check the pages render successfully
and reflect ingested data, not auth behavior (covered elsewhere)."""

from tests.conftest import post_readiness, post_session


def test_dashboard_renders_with_no_data(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "<h1>Alerts</h1>" in response.text


def test_dashboard_reflects_ingested_session(app_client) -> None:
    response = app_client.get("/", params={"today": "2025-04-20"})
    assert "Only 0 session(s) in last 4 weeks" in response.text

    post_session(app_client)  # SESSION_MD: dated 2025-04-15
    response = app_client.get("/", params={"today": "2025-04-20"})
    assert response.status_code == 200
    assert "Only 1 session(s) in last 4 weeks" in response.text


def test_dashboard_readiness_section_shows_empty_state_with_no_data(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "No readiness measurements synced yet" in response.text


def test_dashboard_readiness_section_reflects_latest_event(app_client) -> None:
    # READINESS_MD: readiness=GOOD, lnRmssd=4.30, dated 2026-08-05.
    post_readiness(app_client)
    response = app_client.get("/")
    assert response.status_code == 200
    assert "GOOD" in response.text
    assert "HRV above baseline. Good day to push intensity." in response.text
    assert 'id="readinessChart"' in response.text


def test_dashboard_readiness_chart_colors_points_by_state(app_client) -> None:
    post_readiness(app_client)
    response = app_client.get("/")
    assert response.status_code == 200
    # The chart colors each point by that day's readiness state, not a
    # fixed value threshold — the state->color map and the 5-state legend
    # must both be present.
    assert "READINESS_STATE_COLOR" in response.text
    assert "pointColors" in response.text
    assert "columnColors" in response.text
    for label in ("Deload recommended", "Light day", "Normal", "Good", "Peak"):
        assert label in response.text


def test_dashboard_has_dedicated_rhythm_and_cardiac_issues_section(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "Rhythm &amp; cardiac issues" in response.text
    # The 4 rhythm-counter charts must live under that section, not mixed
    # into the general recovery/fitness metrics.
    for chart_id in ("chart-afibEpisodes", "chart-pauses", "chart-premature", "chart-uneven"):
        assert f'id="{chart_id}"' in response.text


def test_dashboard_charts_have_a_description(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "atrial fibrillation" in response.text  # afib chart description
    assert "Lower means better cardiovascular fitness and recovery." in response.text  # restingHr


def test_dashboard_vo2max_and_bmi_charts_have_no_description(app_client) -> None:
    # Explicitly requested: these keep their chart but drop the descriptive
    # paragraph most other charts have.
    response = app_client.get("/")
    assert response.status_code == 200
    for wrap_id in ("wrap-vo2maxChart", "wrap-chart-bmi", "wrap-chart-bodyComp"):
        assert f'id="{wrap_id}"' in response.text
        section = response.text.split(f'id="{wrap_id}"')[1].split("</div>")[0]
        assert "chart-desc" not in section


def test_dashboard_pauses_chart_has_diagnostic_description(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "2s btw beats pauses" in response.text
    assert "worth mentioning to a doctor" in response.text


def test_dashboard_includes_all_weekly_cardio_charts(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    for chart_id in (
        "chart-restingHr",
        "chart-hrr60s",
        "chart-rmssd",
        "chart-sdnn",
        "chart-pnn50",
        "chart-drift",
        "chart-poincareRatio",
        "chart-afibEpisodes",
        "chart-pauses",
        "chart-premature",
        "chart-uneven",
        "chart-bmi",
        "chart-bodyComp",  # combined body-fat%/lean-mass% chart, not two separate ones
    ):
        assert f'id="{chart_id}"' in response.text


def test_dashboard_legend_is_disabled(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "legend: { display: false }" in response.text


def test_dashboard_charts_have_direction_arrows(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    # restingHr is "lower is better"; hrr60s is "higher is better".
    assert '"restingHr"' in response.text or "restingHr:" in response.text
    assert 'good: "down"' in response.text
    assert 'good: "up"' in response.text


def test_dashboard_charts_include_linear_regression_trend_line(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "linearRegressionLine" in response.text
    assert "(trend)" in response.text


def test_dashboard_body_composition_is_a_single_combined_chart(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert 'id="chart-bodyComp"' in response.text
    # One visible body-fat% line, filled red below (down to 0%) and green
    # above (up to 100%) via two overlapping datasets sharing the same
    # values — the second exists only to fill upward, per the fillColor/
    # hidden_line mechanism in base.html's lineChart().
    assert "Body fat %" in response.text
    assert "#e05a5a40" in response.text  # red fill, down to axis min
    assert "#3ecf7e40" in response.text  # green fill, up to axis max
    assert "hidden_line" in response.text


def test_dashboard_charts_show_empty_state_when_series_is_all_null(app_client) -> None:
    # Client-side JS logic (no browser in this test suite to verify the
    # rendered DOM) — just confirm the empty-state branch is actually wired
    # into the shared lineChart() helper every chart calls.
    response = app_client.get("/")
    assert response.status_code == 200
    assert "No data yet" in response.text
    assert "chart-no-data" in response.text


def test_exercises_list_empty(app_client) -> None:
    response = app_client.get("/exercises")
    assert response.status_code == 200
    assert "No exercises synced yet" in response.text


def test_exercises_list_reflects_ingested_session(app_client) -> None:
    post_session(app_client)
    response = app_client.get("/exercises")
    assert response.status_code == 200
    assert "Bench Press" in response.text
    assert 'href="/exercises/ex-a1b2c3d4"' in response.text


def test_exercise_detail_no_data(app_client) -> None:
    response = app_client.get("/exercises/ex-nonexistent")
    assert response.status_code == 200
    assert "No qualifying set found" in response.text


def test_exercise_detail_reflects_ingested_session(app_client) -> None:
    # SESSION_MD's ex-a1b2c3d4 has excludeFromTonnage: true -> no all-time PR,
    # but recent_heaviest_set_pr doesn't filter on that, so it should show up.
    post_session(app_client)
    response = app_client.get("/exercises/ex-a1b2c3d4")
    assert response.status_code == 200
    assert "Bench Press" in response.text
    assert "No qualifying set found" in response.text  # excludeFromTonnage excludes it


def test_exercise_detail_includes_history_chart_when_data_present(app_client) -> None:
    # SESSION_MD's ex-a1b2c3d4 has excludeFromTonnage: true, which
    # exercise_history filters out (same rule all_time_tonnage_pr uses) — so
    # a plain post_session() wouldn't produce any history points. Send a
    # standalone session with a non-excluded exercise instead.
    from tests.conftest import content_hash_of, post_session

    md = (
        b"---\n"
        b"id: eeeeeeee\n"
        b"date: 2025-04-15\n"
        b"exercises:\n"
        b"  - exerciseId: ex-a1b2c3d4\n"
        b"    exerciseName: Bench Press\n"
        b"    type: FORZA\n"
        b"    sets:\n"
        b"      - reps: 8\n"
        b"        weight: 80.0\n"
        b"---\n"
    )
    post_session(
        app_client,
        md,
        session_id="eeeeeeee",
        rel_path="2025/04/eeeeeeee.md",
        content_hash=content_hash_of(md),
    )

    response = app_client.get("/exercises/ex-a1b2c3d4", params={"today": "2025-04-20"})
    assert response.status_code == 200
    assert 'id="historyChart"' in response.text


def test_exercise_detail_shows_empty_state_when_no_history(app_client) -> None:
    response = app_client.get("/exercises/ex-nonexistent")
    assert response.status_code == 200
    assert "No sets logged for this exercise" in response.text


def test_static_chartjs_is_served(app_client) -> None:
    response = app_client.get("/static/chart.umd.min.js")
    assert response.status_code == 200
    assert len(response.content) > 1000


def test_static_annotation_plugin_is_served(app_client) -> None:
    response = app_client.get("/static/chartjs-plugin-annotation.min.js")
    assert response.status_code == 200
    assert len(response.content) > 1000


def test_dashboard_includes_threshold_reference_lines(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "chartjs-plugin-annotation.min.js" in response.text
    # Non-emoji geometric glyph, per spec — not ⚠ or similar pictographs.
    # Both severities use the same filled ▲; color (not shape) distinguishes
    # warning from danger.
    assert "▲" in response.text
    assert "Tachycardia" in response.text
    assert "Cole et al." in response.text


def test_dashboard_includes_multi_level_thresholds(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    # VO2max's ACSM 6-tier scale.
    assert "Poor &lt; 25" in response.text or "Poor < 25" in response.text
    assert "Superior" in response.text
    # Poincaré ratio's shaded optimal-range band.
    assert "THRESHOLD_BANDS" in response.text
    assert "poincareRatio" in response.text


def test_dashboard_bmi_and_weight_have_cdc_who_thresholds(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    # BMI chart: CDC/WHO cutoffs used directly.
    assert "Obese" in response.text
    assert "Overweight" in response.text
    assert "CDC/WHO" in response.text
    # Weight chart: same cutoffs converted to kg via an assumed fixed
    # height, since this app stores no real height.
    assert "ASSUMED_HEIGHT_M" in response.text
    assert "weightThresholdsFromBmi" in response.text


def test_dashboard_body_fat_chart_has_ace_thresholds(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    # ACE's men's-table body fat % classification.
    assert "Essential fat" in response.text
    assert "Athletes" in response.text
    assert "ACE, men's table" in response.text
