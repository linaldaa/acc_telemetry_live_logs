"""
dashboard.py
============
Post-session dashboard for the ACC Telemetry & Virtual Race Engineer system.

Reads the SQLite database produced by logger.py and renders two persona views:

  * Module A — Tactical View (Driver):     micro-technique correction (Events)
  * Module B — Strategic View (Engineer):  macro strategy & forecasting (Laps)

Run with:
    streamlit run dashboard.py
    streamlit run dashboard.py -- --db telemetry.db
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

import altair as alt          # bundled with Streamlit; no extra dependency
import pandas as pd
import streamlit as st

# Thresholds shared with the logger's tactical analysis
HOT_BRAKE_TEMP = 680.0
TANK_RESERVE = 2.0


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
def get_db_path() -> str:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="telemetry.db")
    # Streamlit passes script args after a bare "--"; ignore anything unknown.
    args, _ = parser.parse_known_args(sys.argv[1:])
    return args.db


@st.cache_data(ttl=10)
def load(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        sessions = pd.read_sql_query("SELECT * FROM Sessions ORDER BY session_id", conn)
        laps = pd.read_sql_query("SELECT * FROM Laps ORDER BY lap_id", conn)
        events = pd.read_sql_query("SELECT * FROM Events ORDER BY event_id", conn)
    finally:
        conn.close()
    return sessions, laps, events


@st.cache_data(ttl=10)
def load_samples(db_path: str, event_id: int) -> pd.DataFrame:
    """Load the raw frames for ONE event (small), or an empty frame.

    Only pulls the selected event's rows rather than the whole Samples table,
    and returns empty if the table doesn't exist (older databases).
    """
    conn = sqlite3.connect(db_path)
    try:
        return pd.read_sql_query(
            "SELECT * FROM Samples WHERE event_id = ? ORDER BY sample_id",
            conn, params=(event_id,),
        )
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def fmt_laptime(seconds: float) -> str:
    if pd.isna(seconds) or seconds <= 0:
        return "--:--.---"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}:{s:06.3f}"


# ---------------------------------------------------------------------------
# Event-detail raw-trace rendering (Altair)
# ---------------------------------------------------------------------------
def _trace_panel(df: pd.DataFrame, channels: dict, y_title: str, title: str,
                 event_span: tuple) -> alt.LayerChart:
    """One stacked panel: several channels vs time, with the EVENT window shaded.

    `df` is the wide samples frame (one row per frame). `channels` maps a column
    name -> the legend label to show for it. We reshape to long/tidy form so
    Altair can colour one line per channel from a single encoding.
    """
    long = df.melt(
        id_vars=["t_rel"], value_vars=list(channels.keys()),
        var_name="channel", value_name="value",
    )
    long["channel"] = long["channel"].map(channels)

    # Light band marking the actual event (between pre-roll and post-roll).
    band = (
        alt.Chart(pd.DataFrame({"start": [event_span[0]], "end": [event_span[1]]}))
        .mark_rect(opacity=0.10, color="#5B8DEF")
        .encode(x="start:Q", x2="end:Q")
    )
    # Vertical rule at t=0 (the instant the event began).
    onset = (
        alt.Chart(pd.DataFrame({"x": [0.0]}))
        .mark_rule(strokeDash=[4, 4], color="#888")
        .encode(x="x:Q")
    )
    lines = (
        alt.Chart(long)
        .mark_line()
        .encode(
            x=alt.X("t_rel:Q", title="Time (s) — 0 = event start"),
            y=alt.Y("value:Q", title=y_title),
            color=alt.Color("channel:N", title=None),
            tooltip=["channel:N", alt.Tooltip("t_rel:Q", format=".2f"),
                     alt.Tooltip("value:Q", format=".2f")],
        )
    )
    return (band + onset + lines).properties(height=170, title=title)


def render_event_detail(db_path: str, ev_row: pd.Series) -> None:
    """Render the stacked raw-trace panels for a single selected event."""
    df = load_samples(db_path, int(ev_row["event_id"]))
    if df.empty:
        st.info(
            "No raw samples for this event. Raw capture only exists for sessions "
            "logged after the feature was added — re-run the logger to populate it."
        )
        return

    # Put t=0 at the moment braking/coasting actually began (first EVENT frame),
    # so the pre-roll is negative time and the event itself is positive.
    event_frames = df[df["phase"] == "EVENT"]
    zero_ms = event_frames["t_ms"].min() if not event_frames.empty else df["t_ms"].min()
    df = df.copy()
    df["t_rel"] = (df["t_ms"] - zero_ms) / 1000.0
    span = (0.0, df.loc[df["phase"] == "EVENT", "t_rel"].max() if not event_frames.empty else 0.0)

    st.caption(
        f"{len(df)} frames · shaded = event window · dashed line = event start. "
        f"Pre-roll {abs(df['t_rel'].min()):.1f}s, post-roll {df['t_rel'].max() - span[1]:.1f}s."
    )

    # Panel 1: driver inputs (brake/throttle/steer overlaid → shows trail-braking)
    st.altair_chart(
        _trace_panel(df, {"brake": "Brake", "throttle": "Throttle", "steer": "Steer"},
                     "Input (0–1) / steer (−1..1)", "Driver inputs", span),
        use_container_width=True,
    )
    # Panel 2: wheel slip (lockups spike well above 1.0)
    st.altair_chart(
        _trace_panel(df, {"slip_fl": "FL", "slip_fr": "FR", "slip_rl": "RL", "slip_rr": "RR"},
                     "Wheel slip ratio", "Wheel slip (lockup > 1.2)", span),
        use_container_width=True,
    )
    # Panel 3: speed
    st.altair_chart(
        _trace_panel(df, {"speed_kmh": "Speed"}, "km/h", "Speed", span),
        use_container_width=True,
    )
    # Panel 4: temperatures, with a toggle between tyre surface and brake disc
    temp_kind = st.radio("Temperature channel", ["Tyre surface", "Brake disc"],
                         horizontal=True, key=f"temp_{ev_row['event_id']}")
    if temp_kind == "Tyre surface":
        cols = {"tyre_fl": "FL", "tyre_fr": "FR", "tyre_rl": "RL", "tyre_rr": "RR"}
        ytitle = "Tyre surface °C"
    else:
        cols = {"btemp_fl": "FL", "btemp_fr": "FR", "btemp_rl": "RL", "btemp_rr": "RR"}
        ytitle = "Brake disc °C"
    st.altair_chart(_trace_panel(df, cols, ytitle, f"{temp_kind} temperature", span),
                    use_container_width=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="ACC Virtual Race Engineer", layout="wide")
    db_path = get_db_path()

    st.title("🏁 ACC Telemetry — Virtual Race Engineer")

    try:
        sessions, laps, events = load(db_path)
    except Exception as exc:
        st.error(f"Could not read database '{db_path}': {exc}")
        st.stop()

    if sessions.empty:
        st.warning("No sessions found yet. Run `python logger.py --sim` to generate data.")
        st.stop()

    # Session picker
    sessions["label"] = (
        "#" + sessions["session_id"].astype(str)
        + " — " + sessions["car_model"].astype(str)
        + " @ " + sessions["track_name"].astype(str)
        + " (" + sessions["date"].astype(str) + ")"
    )
    chosen = st.sidebar.selectbox(
        "Session", sessions["session_id"], format_func=
        lambda sid: sessions.loc[sessions.session_id == sid, "label"].iloc[0],
        index=len(sessions) - 1,
    )
    if st.sidebar.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()

    s_laps = laps[laps.session_id == chosen].copy()
    lap_ids = set(s_laps.lap_id)
    s_events = events[events.lap_id.isin(lap_ids)].copy()

    tactical, strategic = st.tabs(["🔧 Tactical View (Driver)", "📈 Strategic View (Engineer)"])

    # ------------------------------------------------------------------ A
    with tactical:
        st.subheader("Module A — Micro-technique correction")
        brake_events = s_events[s_events.event_type == "BRAKING"].copy()

        if brake_events.empty:
            st.info("No braking events logged for this session yet.")
        else:
            # Join lap numbers for context
            brake_events = brake_events.merge(
                s_laps[["lap_id", "lap_number"]], on="lap_id", how="left"
            )
            overheats = brake_events[brake_events.peak_value > HOT_BRAKE_TEMP]
            lockups = brake_events[brake_events.detail.str.contains("lockup=True", na=False)]

            c1, c2, c3 = st.columns(3)
            c1.metric("Braking events", len(brake_events))
            c2.metric("Brake overheats (>680°C)", len(overheats))
            c3.metric("Lockups detected", len(lockups))

            st.markdown("**Thermal threshold breaches by corner**")
            by_corner = (
                brake_events.groupby("location")["peak_value"]
                .max()
                .sort_values(ascending=False)
            )
            st.bar_chart(by_corner, y_label="Peak brake temp (°C)")

            st.markdown("**Braking event log**")
            show = brake_events[
                ["lap_number", "location", "peak_value", "duration", "detail"]
            ].rename(columns={
                "lap_number": "Lap", "location": "Corner",
                "peak_value": "Peak brake °C", "duration": "Duration (s)",
                "detail": "Analysis",
            })
            st.dataframe(show, use_container_width=True, hide_index=True)

        fuel_saves = s_events[s_events.event_type == "FUEL_SAVE"]
        if not fuel_saves.empty:
            st.markdown("**Fuel-save (lift & coast) events**")
            fs = fuel_saves.merge(s_laps[["lap_id", "lap_number"]], on="lap_id", how="left")
            fs = fs[["lap_number", "location", "peak_value", "duration"]].rename(columns={
                "lap_number": "Lap", "location": "Sector",
                "peak_value": "Coast gap (ms)", "duration": "Duration (s)",
            })
            st.dataframe(fs, use_container_width=True, hide_index=True)

        # -- Event detail: raw telemetry trace --------------------------
        st.divider()
        st.markdown("### 🔬 Event detail — raw trace")
        if s_events.empty:
            st.info("No events to inspect yet.")
        else:
            ev_all = s_events.merge(s_laps[["lap_id", "lap_number"]], on="lap_id", how="left")
            # Newest-first so the most recent driving is easiest to reach.
            ev_all = ev_all.sort_values("event_id", ascending=False)
            ev_all["label"] = (
                "Lap " + ev_all["lap_number"].fillna(0).astype(int).astype(str)
                + " · " + ev_all["event_type"]
                + " · " + ev_all["location"].astype(str)
                + "  (#" + ev_all["event_id"].astype(str) + ")"
            )
            picked = st.selectbox(
                "Pick an event to see its raw telemetry", ev_all["event_id"],
                format_func=lambda eid: ev_all.loc[ev_all.event_id == eid, "label"].iloc[0],
            )
            render_event_detail(db_path, ev_all[ev_all.event_id == picked].iloc[0])

    # ------------------------------------------------------------------ B
    with strategic:
        st.subheader("Module B — Macro strategy & forecasting")
        if s_laps.empty:
            st.info("No completed laps logged for this session yet.")
        else:
            valid = s_laps[s_laps.lap_time > 0].copy()
            avg = valid.lap_time.mean() if not valid.empty else 0
            sd = valid.lap_time.std(ddof=0) if len(valid) > 1 else 0.0
            # Relative spread (stdev / avg) keeps the EMS comparable across
            # short and long tracks (Spa vs Nordschleife).
            cv = sd / avg if avg > 0 else 0.0
            ems = max(0.0, 100.0 - cv * 6000.0)
            avg_fuel = valid.fuel_used[valid.fuel_used > 0].mean() if not valid.empty else 0
            laps_left = ((s_laps.fuel_end.iloc[-1] - TANK_RESERVE) / avg_fuel) \
                if avg_fuel and avg_fuel > 0 else float("nan")

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Avg lap", fmt_laptime(avg))
            c2.metric("Consistency σ", f"{sd:.3f}s")
            c3.metric("EMS score", f"{ems:.0f}/100")
            c4.metric("Est. laps left", f"{laps_left:.0f}" if laps_left == laps_left else "n/a")

            st.markdown("**Lap times**")
            lt = valid.set_index("lap_number")[["lap_time"]].rename(
                columns={"lap_time": "Lap time (s)"})
            st.line_chart(lt, y_label="Lap time (s)")

            st.markdown("**Fuel burned per lap & remaining fuel (pit-window view)**")
            fuel_df = valid.set_index("lap_number")[["fuel_used", "fuel_end"]].rename(
                columns={"fuel_used": "Fuel burned (L)", "fuel_end": "Fuel remaining (L)"})
            st.line_chart(fuel_df)

            st.caption(
                f"Endurance Management Score grades consistency: 100 = metronomic, "
                f"lower = more lap-time scatter (σ = {sd:.3f}s)."
            )

            st.markdown("**Lap log**")
            show = valid[["lap_number", "lap_time", "fuel_used", "fuel_end"]].copy()
            show["lap_time"] = show["lap_time"].map(fmt_laptime)
            show = show.rename(columns={
                "lap_number": "Lap", "lap_time": "Lap time",
                "fuel_used": "Fuel used (L)", "fuel_end": "Fuel remaining (L)",
            })
            st.dataframe(show, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
