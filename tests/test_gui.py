"""Server-rendered dashboard routes — no auth (see app/gui.py's module
docstring for why), so these tests just check the pages render successfully
and reflect ingested data, not auth behavior (covered elsewhere)."""

from tests.conftest import SESSION_MD, post_readiness, post_session


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


def test_dashboard_progressive_overload_omits_rows_with_no_data_on_either_metric(
    app_client,
) -> None:
    """A single ingested session has nothing to compare against (no prior
    week trained) — progressive_overload_verdict returns INSUFFICIENT_DATA
    on both e1RM and tonnage, so the row would render as "(Not enough
    data)" in every column. Such a row is noise, not signal, and must be
    omitted from the table entirely rather than shown."""
    post_session(app_client)  # SESSION_MD: dated 2025-04-15, only session for ex-a1b2c3d4
    response = app_client.get("/", params={"today": "2025-04-20"})
    assert response.status_code == 200
    assert "Not enough data" not in response.text


def test_dashboard_readiness_chart_renders_with_no_data(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert 'id="wrap-readinessChart"' in response.text


def test_dashboard_readiness_chart_reflects_latest_event(app_client) -> None:
    # READINESS_MD: readiness=GOOD, dated 2026-08-05.
    post_readiness(app_client)
    response = app_client.get("/")
    assert response.status_code == 200
    assert 'id="readinessChart"' in response.text


def test_dashboard_readiness_chart_plots_state_not_a_number(app_client) -> None:
    post_readiness(app_client)
    response = app_client.get("/")
    assert response.status_code == 200
    # The chart plots the phone's own classification directly (an ordinal
    # category axis: DELOAD_RECOMMENDED..PEAK), not a recalculated number.
    assert "READINESS_STATE_COLOR" in response.text
    assert "READINESS_LEVELS" in response.text


def test_dashboard_has_dedicated_rhythm_and_cardiac_issues_section(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "Rhythm &amp; cardiac issues" in response.text
    # The 4 rhythm-counter charts must live under that section, not mixed
    # into the general recovery/fitness metrics.
    for chart_id in ("chart-afibEpisodes", "chart-pauses", "chart-premature", "chart-uneven"):
        assert f'id="{chart_id}"' in response.text


def test_dashboard_has_training_load_section_with_acwr_chart(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert "Training load" in response.text
    assert 'id="chart-acwr"' in response.text


def test_dashboard_acwr_chart_reflects_ingested_session(app_client) -> None:
    post_session(app_client)  # SESSION_MD: totalTonnage=12450.0, dated 2025-04-15
    # The chart samples one point per Monday; 2025-04-21 is the first Monday
    # on/after SESSION_MD's date (2025-04-15, a Tuesday).
    response = app_client.get("/", params={"today": "2025-04-21"})
    assert response.status_code == 200
    # A single session is both the entire acute and chronic window -> acwr == 1.0,
    # shows up in the lineChart() call feeding chart-acwr.
    idx = response.text.rfind('"chart-acwr"')
    assert "1.0" in response.text[idx : idx + 2000]


def test_dashboard_training_load_section_includes_rpe_acwr_chart(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert 'id="chart-rpeAcwr"' in response.text


def test_dashboard_rpe_acwr_chart_uses_wider_heuristic_band_not_tonnage_cutoffs(
    app_client,
) -> None:
    # Regression test: an earlier version reused the tonnage ACWR's
    # 0.8/1.3/1.5/0.5 literature-derived thresholds on this chart too, which
    # is unvalidated for a subjective RPE×duration ratio. Current behavior
    # (ANALYSIS_SPEC.md §13.1, correction 2): a deliberately WIDER, separate
    # heuristic band (0.7-1.4/1.7/0.4), explicitly not the tonnage numbers.
    response = app_client.get("/")
    assert response.status_code == 200
    idx = response.text.rfind('"chart-rpeAcwr"')
    call = response.text[idx : idx + 600]
    assert "not clinical" in call
    assert "0.8" not in call  # tonnage ACWR's green-band lower bound must not leak in here
    assert "1.5" not in call  # tonnage ACWR's yellow/red boundary must not leak in here


def test_dashboard_rpe_acwr_chart_reflects_ingested_session_load(app_client) -> None:
    md = SESSION_MD.replace("hrr60s: 28\n", "hrr60s: 28\nsessionRpe: 7\nsessionLoad: 315.0\n")
    post_session(app_client, md_bytes=md.encode())
    # The chart samples one point per Monday; 2025-04-21 is the first Monday
    # on/after SESSION_MD's date (2025-04-15, a Tuesday) so that week's
    # sample point actually sees the session in its acute window.
    response = app_client.get("/", params={"today": "2025-04-21"})
    assert response.status_code == 200
    # A single session is both the entire acute and chronic window for the
    # RPE-ACWR series too -> acwr == 1.0 shows up in the lineChart() call
    # feeding the chart-rpeAcwr canvas (the second/JS occurrence of the id,
    # not the first/markup one).
    idx = response.text.rfind('"chart-rpeAcwr"')
    assert "1.0" in response.text[idx : idx + 2000]


def test_dashboard_training_load_section_includes_session_load_chart(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert 'id="chart-sessionLoad"' in response.text


def test_dashboard_session_load_chart_reflects_ingested_rpe(app_client) -> None:
    md = SESSION_MD.replace("hrr60s: 28\n", "hrr60s: 28\nsessionRpe: 7\nsessionLoad: 315.0\n")
    post_session(app_client, md_bytes=md.encode())
    response = app_client.get("/", params={"today": "2025-04-20"})
    assert response.status_code == 200
    assert "315.0" in response.text


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


def test_dashboard_has_steps_weekly_chart(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert 'id="chart-stepsAvgPerDay"' in response.text
    assert "Weekly median of the daily step average" in response.text


def test_dashboard_steps_chart_has_tudor_locke_thresholds_times_seven(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    idx = response.text.find("stepsAvgPerDay: [")
    assert idx != -1
    block = response.text[idx : idx + 700]
    assert "Tudor-Locke" in block
    # Daily cutoffs ×7 for the weekly view, per the user's own framing.
    assert "12500 * 7" in block
    assert "10000 * 7" in block
    assert "7500 * 7" in block
    assert "5000 * 7" in block
    assert "Sedentary" in block
    assert "Highly active" in block


def test_dashboard_steps_chart_reflects_ingested_readiness_event(app_client) -> None:
    post_readiness(app_client, steps_avg_per_day=8214.5, steps_days_spanned=1)
    response = app_client.get("/", params={"today": "2026-08-10"})
    assert response.status_code == 200
    idx = response.text.rfind('"chart-stepsAvgPerDay"')
    call = response.text[idx : idx + 1000]
    assert "8214.5" in call


def test_dashboard_steps_chart_leaves_no_data_weeks_null_not_zero(app_client) -> None:
    # No readiness events synced at all -> every weekly bucket has n=0
    # readings and must stay null, never render as a literal 0 (which would
    # misread as "measured, zero steps" instead of "not measured").
    response = app_client.get("/")
    assert response.status_code == 200
    idx = response.text.rfind('"chart-stepsAvgPerDay"')
    call = response.text[idx : idx + 1000]
    assert "null" in call


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
        "chart-dfaAlpha1",
        "chart-hrvLfHf",
        "chart-sampleEntropy",
        "chart-hrr1min",
        "chart-hrr2min",
        "chart-morningRestingHr",
        "chart-morningVo2max",
        "chart-hrZones",
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


def test_exercise_detail_shows_bodyweight_load_card_with_audit_fields(app_client) -> None:
    from tests.conftest import content_hash_of, post_session

    md = (
        b"---\n"
        b"id: bbbbbbb1\n"
        b"date: 2026-08-20\n"
        b"exercises:\n"
        b"  - exerciseId: ex-squat01\n"
        b"    exerciseName: Squat\n"
        b"    type: FORZA\n"
        b"    sets:\n"
        b"      - reps: 10\n"
        b"        weight: 82.0\n"
        b"        isBodyweight: true\n"
        b"        bwLoadPercent: 100\n"
        b"        bwBaseWeightKg: 82.0\n"
        b"---\n"
    )
    post_session(
        app_client,
        md,
        session_id="bbbbbbb1",
        rel_path="2026/08/bbbbbbb1.md",
        content_hash=content_hash_of(md),
    )

    response = app_client.get("/exercises/ex-squat01")
    assert response.status_code == 200
    assert "Bodyweight load" in response.text
    assert "100% &times; 82.0 kg" in response.text
    assert "2026-08-20" in response.text


def test_exercise_detail_hides_bodyweight_load_card_for_plain_weighted_exercise(app_client) -> None:
    post_session(app_client)
    response = app_client.get("/exercises/ex-a1b2c3d4")
    assert response.status_code == 200
    assert "Bodyweight load" not in response.text


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


def test_dashboard_header_has_weekly_report_link(app_client) -> None:
    response = app_client.get("/")
    assert response.status_code == 200
    assert 'href="/weekly-report"' in response.text


def test_weekly_report_preview_renders_with_no_data(app_client) -> None:
    response = app_client.get("/weekly-report")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Nessuna sessione questa settimana" in response.text


def test_weekly_report_preview_accepts_explicit_week_end(app_client) -> None:
    post_session(app_client)  # SESSION_MD: dated 2025-04-15
    response = app_client.get("/weekly-report", params={"week_end": "2025-04-20"})
    assert response.status_code == 200
    assert "2025-04-14" in response.text  # the Monday of that week
    assert "2025-04-20" in response.text
