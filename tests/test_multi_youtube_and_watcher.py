"""Tests for multi-account YouTube linking, YouTube Live + Twitch parsing, KreekCraft GTA 6 campaign format, logo overlay, and Posts dashboard."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import main, watcher, youtube


def test_youtube_live_and_twitch_url_parsing():
    yt1 = watcher.parse_stream_info("https://www.youtube.com/live/dy-oCN2NxfA")
    assert yt1["platform"] == "youtube" and yt1["channel"] == "kreekcraft" and yt1["yt_video_id"] == "dy-oCN2NxfA"

    yt2 = watcher.parse_stream_info("https://www.youtube.com/@Kreek/streams")
    assert yt2["platform"] == "youtube" and yt2["channel"] == "kreekcraft"

    tw1 = watcher.parse_stream_info("https://www.twitch.tv/kreekcraft/videos")
    assert tw1["platform"] == "twitch" and tw1["channel"] == "kreekcraft"


def test_multi_account_link_switch_and_selective_disconnect(monkeypatch, tmp_path):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "secret")
    monkeypatch.setattr(youtube, "TOKEN_PATH", tmp_path / "youtube_token.json")
    monkeypatch.setattr(youtube, "_pending", {})
    monkeypatch.setattr(youtube, "_revoke", lambda token: None)

    class Creds:
        def __init__(self, tok):
            self.tok = tok

        def to_json(self):
            return json.dumps({"token": self.tok, "refresh_token": f"r_{self.tok}"})

    # Link Account 1 (Main Channel)
    url1 = youtube.start_auth("http://127.0.0.1:8765/")
    youtube.set_return_to(url1, "/watcher")
    state1 = parse_qs(urlparse(url1).query)["state"][0]
    assert youtube.get_return_to(state1) == "/watcher"
    flow1 = youtube._pending[state1]["flow"]
    monkeypatch.setattr(flow1, "fetch_token", lambda **kw: None)
    monkeypatch.setattr(type(flow1), "credentials", property(lambda self: Creds("t1")))
    monkeypatch.setattr(youtube, "_fetch_channel", lambda creds: {"id": "UC_MAIN", "title": "Main Channel"})
    youtube.finish_auth(state1, "code1")

    # Link Account 2 (Clips Channel)
    url2 = youtube.start_auth("http://127.0.0.1:8765/")
    state2 = parse_qs(urlparse(url2).query)["state"][0]
    flow2 = youtube._pending[state2]["flow"]
    monkeypatch.setattr(flow2, "fetch_token", lambda **kw: None)
    monkeypatch.setattr(type(flow2), "credentials", property(lambda self: Creds("t2")))
    monkeypatch.setattr(youtube, "_fetch_channel", lambda creds: {"id": "UC_CLIPS", "title": "Clips Channel"})
    youtube.finish_auth(state2, "code2")

    accs = youtube.list_accounts()
    assert len(accs["accounts"]) == 2
    assert accs["active_id"] == "UC_CLIPS"
    assert youtube.status()["channel"]["id"] == "UC_CLIPS"

    switched = youtube.select_account("UC_MAIN")
    assert switched["active_id"] == "UC_MAIN"
    assert youtube.status()["channel"]["id"] == "UC_MAIN"

    youtube.disconnect("UC_MAIN")
    after = youtube.list_accounts()
    assert len(after["accounts"]) == 1
    assert after["active_id"] == "UC_CLIPS"
    assert youtube.status()["connected"] is True

    youtube.disconnect("UC_CLIPS")
    assert youtube.status()["connected"] is False
    assert youtube.list_accounts()["accounts"] == []


def test_kreekcraft_campaign_metadata_overlay_and_youtube_publish(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher.cw_config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(watcher, "OUTPUT_DIR", tmp_path)
    assets_dir = tmp_path / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(watcher.cw_config, "ASSETS_DIR", assets_dir)
    monkeypatch.setattr(watcher, "ASSETS_DIR", assets_dir)

    mp4 = tmp_path / "kreekcraft_20261121_001000.mp4"
    srt = tmp_path / "kreekcraft_20261121_001000.srt"
    mp4.write_bytes(b"\0" * 1024)
    srt.write_text(
        "1\n00:00:00,500 --> 00:00:02,000\nWHAT AN INSANE GTA 6 POLICE CHASE!\n\n"
        "2\n00:00:02,100 --> 00:00:04,000\nCHAT IS GOING CRAZY.\n",
        encoding="utf-8",
    )

    client = TestClient(main.app)

    # Upload official logo overlay via API
    ov_res = client.post(
        "/api/watcher/overlay",
        files={"file": ("kreek_logo.png", b"\x89PNG\r\n\x1a\nfake", "image/png")},
    )
    assert ov_res.status_code == 200 and ov_res.json()["present"] is True

    clips = watcher.controller.list_clips()
    assert len(clips) == 1
    c = clips[0]
    assert "kreekcraft" in c["title"].lower()
    assert "Watch KreekCraft on YouTube and Twitch" in c["description"]
    assert "http" not in c["description"] and "twitch.tv/" not in c["description"]
    assert set(c["hashtags"]) >= {"GTA6", "KreekCraft", "GrandTheftAutoVI"}
    assert c["compliance"]["compliant"] is True

    queued = []
    monkeypatch.setattr(main._upload_pool, "submit", lambda fn, *a: queued.append((fn, a)))
    monkeypatch.setattr(
        youtube,
        "status",
        lambda: {"configured": True, "connected": True, "channel": {"id": "UC1", "title": "My Shorts"}},
    )

    # Even if add_source_link=True is sent, campaign mode must strip links from the final description
    res = client.post(
        f"/api/watcher/clips/{mp4.name}/youtube",
        json={
            "title": c["title"],
            "description": c["description"] + " https://www.twitch.tv/kreekcraft",
            "hashtags": c["hashtags"],
            "privacy": "public",
            "made_for_kids": False,
            "notify_subscribers": True,
            "add_source_link": True,
            "account_id": "UC_CLIPS",
        },
    )
    assert res.status_code == 200
    assert res.json()["status"] == "queued"
    assert len(queued) == 1
    _, args = queued[0]
    meta = args[2]
    assert "http" not in meta["snippet"]["description"]
    assert "Watch KreekCraft on YouTube and Twitch" in meta["snippet"]["description"]
    assert "#GTA6 #KreekCraft #GrandTheftAutoVI" in meta["snippet"]["description"]

    del_res = client.delete(f"/api/watcher/clips/{mp4.name}")
    assert del_res.status_code == 409


def test_posts_dashboard_details_live_stats_and_auto_post(monkeypatch, tmp_path):
    monkeypatch.setattr(watcher.cw_config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(watcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "_jobs", {})

    mp4 = tmp_path / "kreekcraft_20261121_010000.mp4"
    srt = tmp_path / "kreekcraft_20261121_010000.srt"
    mp4.write_bytes(b"\0" * 1024)
    srt.write_text("1\n00:00:00,100 --> 00:00:02,000\nCLUTCH WIN RIGHT HERE!\n", encoding="utf-8")

    job_id = "0000000099"
    d = main._job_dir(job_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "clip_01.mp4").write_bytes(b"\0" * 100)
    main._jobs[job_id] = {
        "id": job_id,
        "url": "https://www.youtube.com/watch?v=xyz123",
        "status": "done",
        "progress": 100,
        "message": "Done",
        "title": "Test Video",
        "created": 10.0,
        "options": {},
        "clips": [{
            "title": "Posted Clip",
            "description": "Watch KreekCraft on YouTube and Twitch! 🔴",
            "hashtags": ["GTA6", "KreekCraft", "GrandTheftAutoVI"],
            "video": f"/media/{job_id}/clip_01.mp4",
            "thumb": "",
            "length": 35,
            "youtube": {"status": "done", "video_id": "vid_abc", "url": "https://youtu.be/vid_abc"},
        }],
    }

    monkeypatch.setattr(
        youtube,
        "status",
        lambda: {"configured": True, "connected": True, "channel": {"id": "UC1", "title": "My Channel"}},
    )
    monkeypatch.setattr(
        youtube,
        "fetch_video_stats",
        lambda ids, **kw: {"vid_abc": {"views": 2000, "likes": 90, "comments": 10}},
    )

    client = TestClient(main.app)
    res = client.get("/api/posts")
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_clips"] == 2
    assert data["summary"]["posted_count"] == 1
    assert data["summary"]["ready_count"] == 1
    assert data["summary"]["total_views"] == 2000
    assert data["summary"]["overall_engagement_rate"] == 5.0
    assert data["summary"]["meets_08_pct"] is True
    assert data["summary"]["estimated_payout_usd"] == 2.0

    patch_res = client.patch(
        "/api/posts/clip",
        json={
            "source": "watcher",
            "name": mp4.name,
            "title": "KreekCraft GTA 6 Police Escape",
            "description": "Watch KreekCraft on YouTube and Twitch! 🔴",
            "hashtags": ["GTA6", "KreekCraft", "GrandTheftAutoVI"],
            "privacy": "public",
            "add_source_link": False,
        },
    )
    assert patch_res.status_code == 200
    updated_items = {it["key"]: it for it in client.get("/api/posts").json()["items"]}
    w_item = updated_items[f"watcher:{mp4.name}"]
    assert w_item["title"] == "KreekCraft GTA 6 Police Escape"
    assert "#GTA6 #KreekCraft #GrandTheftAutoVI" in w_item["preview"]["final_description"]
    assert "http" not in w_item["preview"]["final_description"]

    queued = []
    monkeypatch.setattr(main._upload_pool, "submit", lambda fn, *a: queued.append((fn, a)))
    auto_res = client.post(
        "/api/posts/auto",
        json={"enabled": True, "privacy": "public", "post_all_ready": True},
    )
    assert auto_res.status_code == 200
    assert auto_res.json()["auto_post"]["enabled"] is True
    assert auto_res.json()["queued_now"] == 1
    assert len(queued) == 1


def test_parallel_backfill_energy_scoring_and_status(monkeypatch, tmp_path):
    import struct
    import wave

    # Create a 40-second WAV that is quiet except for a loud burst between 12s and 18s
    wav_path = tmp_path / "chunk.backfill.wav"
    sr = 16000
    samples = []
    for sec in range(40):
        amp = 20000 if 12 <= sec < 18 else 200
        samples.extend([amp, -amp] * (sr // 2))
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))

    # Patch clipper.probe_duration and subprocess.run inside backfill.pick_best_30s_offset
    monkeypatch.setattr(watcher.clipper, "probe_duration", lambda p: 40.0)
    monkeypatch.setattr(watcher.backfill.subprocess, "run", lambda *a, **kw: None)
    dummy_vid = tmp_path / "chunk.mp4"
    dummy_vid.write_bytes(b"mp4")
    best_offset = watcher.backfill.pick_best_30s_offset(dummy_vid, clip_len=30.0)
    assert 0.0 <= best_offset <= 10.0

    st = watcher.controller.status()
    assert "backfill" in st and "missed_seconds" in st["backfill"]


def test_multi_social_tokens_platform_payloads_approval_and_sequential_loop(monkeypatch, tmp_path):
    from app import socials

    monkeypatch.setattr(socials, "SOCIAL_ACCOUNTS_PATH", tmp_path / "social_accounts.json")
    monkeypatch.setattr(watcher.cw_config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(watcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "_jobs", {})

    mp4 = tmp_path / "kreekcraft_20261121_050000.mp4"
    srt = tmp_path / "kreekcraft_20261121_050000.srt"
    mp4.write_bytes(b"\0" * 2048)
    srt.write_text("1\n00:00:00,100 --> 00:00:02,000\nINSANE GTA 6 JET STUNT!\n", encoding="utf-8")

    client = TestClient(main.app)

    # 1. Connect TikTok and Instagram accounts via dashboard token API
    tt_res = client.post(
        "/api/socials/accounts",
        json={
            "platform": "tiktok",
            "title": "@kreekclips_tt",
            "access_token": "act.tiktok_test_token",
            "open_id": "oid_123",
        },
    )
    assert tt_res.status_code == 200
    assert tt_res.json()["tiktok"]["connected"] is True

    ig_res = client.post(
        "/api/socials/accounts",
        json={
            "platform": "instagram",
            "title": "@kreekreels_ig",
            "ig_user_id": "17841400000999",
            "access_token": "EAAB_ig_test_token",
        },
    )
    assert ig_res.status_code == 200
    assert ig_res.json()["instagram"]["connected"] is True

    # 2. Update platform-specific fields for YouTube, TikTok, and Instagram
    key = f"watcher:{mp4.name}"
    patch_res = client.patch(
        "/api/posts/clip",
        json={
            "source": "watcher",
            "name": mp4.name,
            "title": "KreekCraft GTA 6 Jet Stunt",
            "description": "Watch KreekCraft on YouTube and Twitch! 🔴",
            "hashtags": ["GTA6", "KreekCraft", "GrandTheftAutoVI"],
            "privacy": "public",
            "add_source_link": False,
            "made_for_kids": False,
            "notify_subscribers": True,
            "target_platforms": ["youtube", "tiktok", "instagram"],
            "tiktok_options": {
                "caption": "Jet Stunt! Watch KreekCraft on YouTube and Twitch! 🔴 #GTA6 #KreekCraft #GrandTheftAutoVI",
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "post_mode": "DIRECT_POST",
                "allow_comment": True,
                "allow_duet": False,
                "allow_stitch": True,
                "cover_timestamp_ms": 1500,
            },
            "instagram_options": {
                "caption": "Jet Stunt! Watch KreekCraft on YouTube and Twitch! 🔴 #GTA6 #KreekCraft #GrandTheftAutoVI",
                "share_to_feed": True,
                "audio_name": "KreekCraft GTA 6 Live",
                "thumb_offset_ms": 2000,
            },
        },
    )
    assert patch_res.status_code == 200

    # 3. Verify platform-dependent payloads
    posts = client.get("/api/posts").json()
    item = next(x for x in posts["items"] if x["key"] == key)
    payloads = item["payloads"]
    assert payloads["youtube"]["body"]["snippet"]["title"] == "KreekCraft GTA 6 Jet Stunt"
    assert payloads["tiktok"]["body"]["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    assert payloads["tiktok"]["body"]["post_info"]["disable_duet"] is True
    assert payloads["tiktok"]["body"]["post_info"]["video_cover_timestamp_ms"] == 1500
    assert payloads["instagram"]["body"]["media_type"] == "REELS"
    assert payloads["instagram"]["body"]["thumb_offset"] == 2000
    assert "17841400000999" in payloads["instagram"]["endpoint"]

    # 4. Approve clip and verify queue position
    app_res = client.post("/api/posts/approve", json={"key": key, "approval_status": "approved"})
    assert app_res.status_code == 200
    posts2 = client.get("/api/posts").json()
    item2 = next(x for x in posts2["items"] if x["key"] == key)
    assert item2["approval_status"] == "approved"
    assert item2["queue_position"] == 1

    # 5. Direct multi-social publish to TikTok & Instagram
    monkeypatch.setattr(
        socials,
        "upload_to_tiktok",
        lambda path, pay, **kw: {"status": "done", "publish_id": "tt_99", "url": "https://www.tiktok.com/@kreekclips_tt"},
    )
    monkeypatch.setattr(
        socials,
        "upload_to_instagram",
        lambda path, pay, **kw: {"status": "done", "media_id": "ig_99", "url": "https://www.instagram.com/kreekreels_ig/reels/"},
    )
    # Run _upload_pool synchronously during test
    monkeypatch.setattr(main._upload_pool, "submit", lambda fn, *a: fn(*a))

    pub_res = client.post("/api/posts/publish", json={"key": key, "platforms": ["tiktok", "instagram"]})
    assert pub_res.status_code == 200
    posts3 = client.get("/api/posts").json()
    item3 = next(x for x in posts3["items"] if x["key"] == key)
    assert item3["tiktok"]["status"] == "done"
    assert item3["instagram"]["status"] == "done"


def test_campaigns_studio_brief_parser_general_mode_and_cliffhanger_hook(monkeypatch, tmp_path):
    from app import campaigns

    monkeypatch.setattr(campaigns, "CAMPAIGNS_PATH", tmp_path / "campaigns.json")
    client = TestClient(main.app)

    # 1. Parse a raw rules brief
    brief_text = """
    Speed GTA 6 Clipping: Full Rules Brief
    CPM: $2.5 per 1,000 views
    Max Payout: $500 per clip
    Engagement Requirement: Every clip must maintain at least a 1.2% engagement rate.
    Caption Requirement: Include "Watch IShowSpeed Live on YouTube" in every caption. Do not include links or URLs in the caption.
    Hashtag Requirement: Every clip must include #IShowSpeed #GTA6 #SpeedLive.
    THE OFFICIAL LOGO OVERLAY MUST APPEAR THROUGHOUT THE ENTIRE VIDEO.
    """
    parsed_res = client.post("/api/campaigns/parse-brief", json={"brief": brief_text})
    assert parsed_res.status_code == 200
    parsed = parsed_res.json()
    assert parsed["cpm_usd"] == 2.5
    assert parsed["max_payout_usd"] == 500.0
    assert parsed["min_engagement_pct"] == 1.2
    assert "Watch IShowSpeed Live on YouTube" in parsed["required_cta"]
    assert set(parsed["required_hashtags"]) >= {"IShowSpeed", "GTA6", "SpeedLive"}
    assert parsed["no_links"] is True
    assert parsed["require_overlay"] is True

    # 2. Save & activate the new campaign profile
    save_res = client.post("/api/campaigns", json=parsed)
    assert save_res.status_code == 200
    active = save_res.json()["active"]
    assert active["name"] == "Speed GTA 6 Clipping"

    # 3. Verify cliffhanger ASS script generation includes CliffBox style & Dialogue event
    srt = tmp_path / "sample.srt"
    srt.write_text("1\n00:00:00,200 --> 00:00:02,000\nNO WAY HE JUMPED THE HELICOPTER!\n", encoding="utf-8")
    meta = watcher.captioner.build_default_metadata(srt, channel="ishowspeed", campaign_mode=True, campaign_rules=active)
    assert "Watch IShowSpeed Live on YouTube" in meta["description"]
    assert meta["cliffhanger_text"] and len(meta["cliffhanger_text"]) <= 42 and "😱" not in meta["cliffhanger_text"]
    ass_text = watcher.captioner._build_cliffhanger_and_captions_ass(srt, meta["cliffhanger_text"], 4.0)
    assert "Style: CliffBox,Arial Black" in ass_text
    assert "Dialogue: 2,0:00:00.00,0:00:04.00,CliffBox" in ass_text

    # 4. Switch to General Mode (non-campaign) and verify clips use standard editing without forced CTA/no-links
    gen_res = client.post("/api/campaigns/select", json={"campaign_id": "general"})
    assert gen_res.status_code == 200
    gen_active = gen_res.json()["active"]
    assert gen_active["id"] == "general"
    gen_meta = watcher.captioner.build_default_metadata(srt, channel="ishowspeed", campaign_mode=False, campaign_rules=gen_active)
    assert gen_meta["campaign_mode"] is False
    assert gen_meta["overlay_applied"] is False
    assert "Watch IShowSpeed Live on YouTube" not in gen_meta["description"]


def test_graceful_resume_queue_clear_and_error_classification(monkeypatch, tmp_path):
    from app import socials

    monkeypatch.setattr(socials, "SOCIAL_ACCOUNTS_PATH", tmp_path / "social_accounts.json")
    monkeypatch.setattr(watcher.cw_config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(watcher, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "_jobs", {})

    mp4 = tmp_path / "kreekcraft_20261121_090000.mp4"
    srt = tmp_path / "kreekcraft_20261121_090000.srt"
    mp4.write_bytes(b"\0" * 2048)
    srt.write_text("1\n00:00:00,100 --> 00:00:02,000\nGRACEFUL RESUME TEST!\n", encoding="utf-8")

    client = TestClient(main.app)
    key = f"watcher:{mp4.name}"

    # 1. Approve clip -> queue_position == 1, approved_unposted_count == 1
    client.post("/api/posts/approve", json={"key": key, "approval_status": "approved"})
    snap1 = client.get("/api/posts").json()
    assert snap1["summary"]["approved_unposted_count"] == 1
    assert snap1["items"][0]["queue_position"] == 1

    # 2. Simulate an upload error on YouTube and verify retry_failed clears it for graceful resume
    main._set_clip_field_by_key(key, youtube={"status": "error", "error": "Transient 503 network error"})
    snap2 = client.get("/api/posts").json()
    assert snap2["summary"]["failed_count"] == 1
    retry_res = client.post("/api/posts/approve", json={"retry_failed": True})
    assert retry_res.status_code == 200 and retry_res.json()["updated"] == 1
    snap3 = client.get("/api/posts").json()
    assert snap3["summary"]["failed_count"] == 0
    assert snap3["items"][0]["youtube"] is None

    # 3. Clear Approved Queue (reset_all_to_pending) -> approved_unposted_count becomes 0
    clr_res = client.post("/api/posts/approve", json={"reset_all_to_pending": True})
    assert clr_res.status_code == 200 and clr_res.json()["updated"] == 1
    snap4 = client.get("/api/posts").json()
    assert snap4["summary"]["approved_unposted_count"] == 0
    assert snap4["items"][0]["approval_status"] == "pending"
    assert snap4["items"][0]["queue_position"] is None

    # 4. Verify error classifier
    assert main._classify_upload_error("YouTube refused the upload (429): Quota exceeded") == "quota"
    assert main._classify_upload_error("TikTok API rejected init (spam_risk_too_many_pending_share)") == "quota"
    assert main._classify_upload_error("TikTok API rejected init (rate_limit_exceeded)") == "transient"
    assert main._classify_upload_error("401: sign-in has expired") == "auth"
    assert main._classify_upload_error("Connection reset by peer") == "transient"


def test_jittered_delay_and_tiktok_inbox_cache(monkeypatch, tmp_path):
    from app import socials

    assert main._compute_jittered_delay(0, 30) == 0
    assert main._compute_jittered_delay(20, 0) == 20
    samples = [main._compute_jittered_delay(20, 30) for _ in range(40)]
    assert all(14 <= s <= 26 for s in samples)

    # Cached force_inbox_draft skips DIRECT_POST probes and uses only 1 init call
    monkeypatch.setattr(socials, "SOCIAL_ACCOUNTS_PATH", tmp_path / "social_accounts.json")
    monkeypatch.setattr(socials, "_last_tt_init_ts", 0.0)
    monkeypatch.setattr(socials.time, "sleep", lambda *_: None)
    socials._write_social_store({
        "youtube": {"active_id": None, "accounts": {}},
        "tiktok": {
            "active_id": "tt1",
            "accounts": {
                "tt1": {
                    "id": "tt1",
                    "title": "@testuser",
                    "access_token": "tok_123",
                    "force_inbox_draft": True,
                    "force_inbox_draft_at": socials.time.time(),
                }
            },
        },
        "instagram": {"active_id": None, "accounts": {}},
    })

    mp4 = tmp_path / "clip.mp4"
    mp4.write_bytes(b"mp4data")

    calls = []

    class FakeResp:
        def __init__(self, code, payload):
            self.status_code = code
            self.content = b"{}"
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, **kw):
        calls.append(url)
        return FakeResp(200, {"error": {"code": "ok"}, "data": {"publish_id": "pub_1", "upload_url": "https://upload.example/1"}})

    def fake_put(url, **kw):
        return FakeResp(200, {})

    monkeypatch.setattr(socials.requests, "post", fake_post)
    monkeypatch.setattr(socials.requests, "put", fake_put)

    res = socials.upload_to_tiktok(mp4, {"post_mode": "DIRECT_POST", "body": {"post_info": {}, "source_info": {}}})
    assert res["status"] == "done"
    assert res["post_mode"] == "MEDIA_UPLOAD"
    assert len(calls) == 1
    assert calls[0].endswith("/inbox/video/init/")


def test_learning_context_and_word_pop_ass(monkeypatch, tmp_path):
    import json
    from app import config, render, selector, source

    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    assert selector._build_learning_context() == ""

    jdir = tmp_path / "jobs" / "job1"
    jdir.mkdir(parents=True)
    (jdir / "job.json").write_text(json.dumps({
        "clips": [
            {"title": "Police Chase Fail", "hashtags": ["GTA6", "shorts"], "live_stats": {"views": 5200, "likes": 310, "comments": 25}},
            {"title": "Slow Menu Clip", "hashtags": ["gaming"], "live_stats": {"views": 18, "likes": 0, "comments": 0}},
        ]
    }), encoding="utf-8")

    ctx = selector._build_learning_context()
    assert "Police Chase Fail" in ctx
    assert "5200 views" in ctx

    ass = render.build_ass([source.Word("WOW", 0.0, 0.4), source.Word("WHAT", 0.4, 0.8)], 0.0, 1.0, "yellow", "WAIT FOR IT")
    assert "\\fscx114\\fscy114\\t(0,80,\\fscx100\\fscy100)" in ass

    import captioner
    hook_txt = captioner.derive_cliffhanger_text("He stole the police car right there!", "WAIT FOR THE END... 😱", "IShowSpeed")
    assert "😱" not in hook_txt and len(hook_txt) <= 42 and "HE STOLE THE POLICE" not in hook_txt





