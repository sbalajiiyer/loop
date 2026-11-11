import os, tempfile
from loop import db
from loop.sources import whoop_api, oura_api

SLEEP = {"id": "s1", "nap": False, "score_state": "SCORED", "start": "2022-04-24T02:25:44.774Z", "end": "2022-04-24T10:25:44.774Z",
         "timezone_offset": "-05:00", "score": {"stage_summary": {"total_in_bed_time_milli": 30272735, "total_awake_time_milli": 1403507,
         "total_light_sleep_time_milli": 14905851, "total_slow_wave_sleep_time_milli": 6630370, "total_rem_sleep_time_milli": 5879573,
         "disturbance_count": 12}, "respiratory_rate": 16.1, "sleep_performance_percentage": 98, "sleep_efficiency_percentage": 91.7}}
REC = {"sleep_id": "s1", "score_state": "SCORED", "score": {"recovery_score": 44, "resting_heart_rate": 64, "hrv_rmssd_milli": 31.8,
       "spo2_percentage": 95.7, "skin_temp_celsius": 33.7, "user_calibrating": False}}
CYC = {"id": 1, "start": "2022-04-24T02:25:44.774Z", "timezone_offset": "-05:00", "score_state": "SCORED",
       "score": {"strain": 5.3, "kilojoule": 8288.3}, "step_count": 8234}

def test_whoop_mapping(monkeypatch):
    pages = {"/v2/activity/sleep": [SLEEP], "/v2/recovery": [REC], "/v2/cycle": [CYC], "/v2/activity/workout": []}
    monkeypatch.setattr(whoop_api.provider, "get", lambda url, params=None: {"records": pages[url.split("/developer")[1]]})
    whoop_api.sync()
    r = db.query("SELECT * FROM daily WHERE device='whoop' AND source='official'")[0]
    assert r["day"] == "2022-04-24" and r["recovery"] == 44 and r["hrv"] == 31.8 and r["strain"] == 5.3
    assert round(r["sleep_min"]) == 457 and r["steps"] == 8234

def test_oura_mapping(monkeypatch):
    data = {"daily_readiness": [{"day": "2026-10-20", "score": 81, "temperature_deviation": -0.1}],
            "sleep": [{"type": "long_sleep", "day": "2026-10-20", "total_sleep_duration": 27000, "time_in_bed": 29000,
                       "deep_sleep_duration": 5400, "rem_sleep_duration": 6000, "light_sleep_duration": 15600, "awake_time": 2000,
                       "efficiency": 93, "average_hrv": 48, "lowest_heart_rate": 52, "average_breath": 14.6,
                       "bedtime_start": "2026-10-19T23:10:00-04:00", "bedtime_end": "2026-10-20T07:13:00-04:00"}],
            "daily_sleep": [{"day": "2026-10-20", "score": 85}], "daily_activity": [{"day": "2026-10-20", "steps": 9000, "total_calories": 2500, "score": 90}],
            "daily_spo2": [{"day": "2026-10-20", "spo2_percentage": {"average": 96.5}}],
            "heartrate": [{"timestamp": "2026-10-20T12:00:00+00:00", "bpm": 70, "source": "awake"}]}
    data["sleep"][0]["heart_rate"] = {"interval": 300.0, "items": [55.0, None, 53.0], "timestamp": "2026-10-19T23:10:00-04:00"}
    data["sleep"][0]["hrv"] = {"interval": 300.0, "items": [40.0, 45.0, None], "timestamp": "2026-10-19T23:10:00-04:00"}
    monkeypatch.setattr(oura_api.provider, "get", lambda url, params=None: {"data": data[url.rsplit("/", 1)[1]]})
    oura_api.sync()
    r = db.query("SELECT * FROM daily WHERE device='oura' AND source='official'")[0]
    assert r["recovery"] == 81 and r["hrv"] == 48 and r["rhr"] == 52 and r["sleep_min"] == 450 and r["spo2"] == 96.5 and r["steps"] == 9000
    assert db.query("SELECT COUNT(*) n FROM hr_samples WHERE device='oura'")[0]["n"] == 3
    assert db.query("SELECT COUNT(*) n FROM hrv_samples WHERE device='oura'")[0]["n"] == 2
