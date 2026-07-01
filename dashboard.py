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


def fmt_laptime(seconds: float) -> str:
    if pd.isna(seconds) or seconds <= 0:
        return "--:--.---"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}:{s:06.3f}"


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
