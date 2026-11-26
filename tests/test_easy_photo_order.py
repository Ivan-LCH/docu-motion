"""간편 자동 ① 사진 삭제·순서 변경: PUT /slides/order, DELETE 뒤 정리(order_index·프로젝트 meta·길이 맞춤),
계획/렌더가 사용자 순서를 그대로 쓰는지, 렌더 중 409.

네트워크·실제 렌더는 없다 (renderer.render_project 를 가로채 슬라이드 순서만 본다)."""
from __future__ import annotations

import json

import pytest

from backend.db.models import Project, Slide
from backend.services import auto_edit, easy_auto, slide_order, task_status
from tests.test_auto_edit import (_sine_wav, _slides, db, make_project, no_network,  # noqa: F401
                                  outputs, session_factory)


def _m(s) -> dict:
    return json.loads(s.meta or "{}")


def _set(db, s, **kv):
    m = _m(s)
    m.update(kv)
    s.meta = json.dumps(m, ensure_ascii=False)
    db.commit()


PROJECT_KV = {"easy_settings": {"resolution": "720p", "stylize_ratio": 0.3, "slide_duration": 3.5,
                                "auto_curate": False},
              "bgm_credit": {"title": "Forest Walk", "artist": "A", "license_url": "", "page_url": "",
                             "jamendo_id": 7, "duration": 40},
              "to_real_hold_off": True, "bgm_loop_xfade": 1.5}


@pytest.fixture
def client(session_factory, outputs, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.api.v1 import slides as slides_api
    from backend.db.session import get_db

    app = FastAPI()
    app.include_router(slides_api.router, prefix="/api/v1")

    def _get_db():
        s = session_factory()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _get_db
    monkeypatch.setattr(slides_api, "OUTPUTS_DIR", outputs)
    return TestClient(app)


@pytest.fixture
def project(db, outputs):
    p = make_project(db, outputs, n_photos=6, place_at=None)
    sl = _slides(db, p.id)
    _set(db, sl[0], easy_place="일월수목원", **PROJECT_KV)
    _set(db, sl[2], easy_feel="단풍 길", to_real_hold=2.0)
    yield p
    task_status.clear(easy_auto.TASK_KEY.format(project_id=p.id))


def _ids(db, pid):
    db.expire_all()
    return [s.id for s in _slides(db, pid)]


# ────────────────────────────────────────────────────────────────────────── #
# 순서 변경
# ────────────────────────────────────────────────────────────────────────── #
def test_reorder_sets_order_index_and_flag(client, db, project):
    ids = _ids(db, project.id)
    new = [ids[3], ids[0], ids[5], ids[1], ids[2], ids[4]]
    r = client.put(f"/api/v1/projects/{project.id}/slides/order", json={"slide_ids": new})
    assert r.status_code == 200
    assert [s["id"] for s in r.json()] == new
    assert _ids(db, project.id) == new
    sl = _slides(db, project.id)
    assert [s.order_index for s in sl] == list(range(6))
    assert _m(sl[0])["user_ordered"] is True


def test_reorder_rejects_mismatch_and_duplicates(client, db, project):
    ids = _ids(db, project.id)
    url = f"/api/v1/projects/{project.id}/slides/order"
    assert client.put(url, json={"slide_ids": ids[:-1]}).status_code == 400            # 누락
    assert client.put(url, json={"slide_ids": ids + ["nope"]}).status_code == 400       # 남의 id
    assert client.put(url, json={"slide_ids": ids[:-1] + [ids[0]]}).status_code == 400  # 중복
    assert client.put("/api/v1/projects/missing/slides/order", json={"slide_ids": []}).status_code == 404
    assert _ids(db, project.id) == ids                                                  # 그대로


def test_reorder_moves_project_meta_to_new_first(client, db, project):
    ids = _ids(db, project.id)
    new = ids[1:] + ids[:1]                                     # 첫 사진을 맨 뒤로
    assert client.put(f"/api/v1/projects/{project.id}/slides/order", json={"slide_ids": new}).status_code == 200
    db.expire_all()
    sl = _slides(db, project.id)
    first, old_first = _m(sl[0]), _m(sl[-1])
    for k, v in PROJECT_KV.items():
        assert first[k] == v
        assert k not in old_first
    assert first["user_ordered"] is True and "user_ordered" not in old_first
    # 슬라이드별 자막·홀드는 자기 슬라이드에 남는다
    assert old_first["easy_place"] == "일월수목원" and "easy_place" not in first
    by_id = {s.id: _m(s) for s in sl}
    assert by_id[ids[2]]["easy_feel"] == "단풍 길" and by_id[ids[2]]["to_real_hold"] == 2.0
    # 읽는 쪽(presets)이 새 첫 슬라이드에서 그대로 찾는다
    from backend.services.presets import project_hold_off
    assert project_hold_off([{"meta": s.meta} for s in sl]) is True
    assert easy_auto.easy_settings(sl)["resolution"] == "720p"


def test_reorder_409_while_easy_render_running(client, db, project):
    ids = _ids(db, project.id)
    task_status.set_status(easy_auto.TASK_KEY.format(project_id=project.id), status="running")
    r = client.put(f"/api/v1/projects/{project.id}/slides/order", json={"slide_ids": ids[::-1]})
    assert r.status_code == 409
    assert _ids(db, project.id) == ids


# ────────────────────────────────────────────────────────────────────────── #
# 삭제
# ────────────────────────────────────────────────────────────────────────── #
def test_delete_compacts_and_removes_files(client, db, outputs, project):
    ids = _ids(db, project.id)
    victim = _slides(db, project.id)[2]
    path = outputs / project.id / "assets" / victim.image_filename
    assert path.exists()
    assert client.delete(f"/api/v1/projects/{project.id}/slides/{ids[2]}").status_code == 204
    assert not path.exists()
    db.expire_all()
    sl = _slides(db, project.id)
    assert [s.id for s in sl] == ids[:2] + ids[3:]
    assert [s.order_index for s in sl] == list(range(5))


def test_delete_first_moves_project_meta(client, db, project):
    ids = _ids(db, project.id)
    assert client.delete(f"/api/v1/projects/{project.id}/slides/{ids[0]}").status_code == 204
    db.expire_all()
    sl = _slides(db, project.id)
    assert sl[0].id == ids[1] and sl[0].order_index == 0
    first = _m(sl[0])
    for k, v in PROJECT_KV.items():
        assert first[k] == v
    assert all(k not in _m(s) for s in sl[1:] for k in PROJECT_KV)


def test_delete_409_while_easy_render_running(client, db, outputs, project):
    ids = _ids(db, project.id)
    victim = _slides(db, project.id)[1]
    task_status.set_status(easy_auto.TASK_KEY.format(project_id=project.id), status="running")
    r = client.delete(f"/api/v1/projects/{project.id}/slides/{ids[1]}")
    assert r.status_code == 409
    assert (outputs / project.id / "assets" / victim.image_filename).exists()
    assert _ids(db, project.id) == ids
    task_status.set_status(easy_auto.TASK_KEY.format(project_id=project.id), status="done")
    assert client.delete(f"/api/v1/projects/{project.id}/slides/{ids[1]}").status_code == 204


def test_delete_clears_fit_and_next_fit_is_shorter(client, db, outputs, project, no_network):
    """곡 = 자연 길이 × 1.1 → adjust 로 사진을 늘림. 2장 지우면 bgm_fit/easy_duration 이 지워지고
    다시 맞추면 자연 길이·맞춘 길이가 짧아진다. 고른 곡(bgm_filename·bgm_credit)은 그대로."""
    L = easy_auto.estimate_length(db, project.id)["length"]
    _sine_wav(outputs / project.id / "assets" / "bgm_test.wav", seconds=L * 1.1)
    auto_edit.plan_auto_edit(db, project.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.0}))
    fit1 = easy_auto.fit_music(db, project.id)
    assert fit1["mode"] == "adjust"
    sl = _slides(db, project.id)
    assert _m(sl[0])["bgm_fit"] == fit1
    assert any("easy_duration" in _m(s) for s in sl)
    ids = [s.id for s in sl]
    for sid in (ids[0], ids[3]):
        assert client.delete(f"/api/v1/projects/{project.id}/slides/{sid}").status_code == 204
    db.expire_all()
    sl = _slides(db, project.id)
    assert "bgm_fit" not in _m(sl[0])
    assert not any("easy_duration" in _m(s) or "easy_duration" in (_m(s).get("auto_fields") or []) for s in sl)
    assert _m(sl[0])["bgm_credit"] == PROJECT_KV["bgm_credit"]
    assert db.query(Project).get(project.id).bgm_filename == "bgm_test.wav"
    est = easy_auto.estimate_length(db, project.id)
    assert est["photos"] == 4
    out = easy_auto.prepare(db, project.id, {"stylize_ratio": 0.0})
    fit2 = out["fit"]
    assert fit2["natural_length"] < fit1["natural_length"]
    assert fit2["mode"] != "adjust" or fit2["scale"] > fit1["scale"]   # 짧아진 만큼 다시 늘리거나 루프
    assert fit2["track_duration"] == fit1["track_duration"]
    assert _m(_slides(db, project.id)[0])["bgm_fit"] == fit2


# ────────────────────────────────────────────────────────────────────────── #
# 계획 / 렌더가 사용자 순서를 그대로 쓴다 (촬영 시각순으로 되돌리지 않음)
# ────────────────────────────────────────────────────────────────────────── #
def test_plan_and_render_follow_user_order(client, db, session_factory, outputs, project,
                                           no_network, monkeypatch):
    from backend.db import session as db_session
    from backend.services import renderer, worker

    ids = _ids(db, project.id)
    # 촬영 시각과 반대 순서 (make_project 사진은 시각 오름차순)
    new = ids[::-1]
    assert client.put(f"/api/v1/projects/{project.id}/slides/order", json={"slide_ids": new}).status_code == 200
    db.expire_all()
    assert _m(_slides(db, project.id)[0]).get("user_ordered") is True, "after reorder"
    by_id = {s.id: s.image_filename for s in _slides(db, project.id)}
    want = [by_id[i] for i in new]

    plan = auto_edit.plan_auto_edit(db, project.id, easy_auto.easy_plan_opts({"stylize_ratio": 0.3}))
    assert _ids(db, project.id) == new
    order_of = {s.id: s.order_index for s in _slides(db, project.id)}
    assert [order_of[t["slide_id"]] for t in plan["stylized"]] == sorted(order_of[t["slide_id"]]
                                                                         for t in plan["stylized"])

    rendered = []

    class _NoTTS:
        def __init__(self, *a, **k):
            pass

        def load_model(self):
            return False

        def unload_model(self):
            pass

    def fake_render(**kw):
        rendered.append([s["image_filename"] for s in kw["slides"]])
        rendered.append(kw["slides"][0]["meta"])

    monkeypatch.setattr(db_session, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(worker, "TTSEngine", _NoTTS)
    monkeypatch.setattr(renderer, "render_project", fake_render)
    easy_auto.run_easy_render(project.id, {"auto_curate": False, "stylize_ratio": 0.3})

    st = task_status.get_status(easy_auto.TASK_KEY.format(project_id=project.id))
    assert st["status"] == "done", st
    assert rendered[0] == want
    first_meta = json.loads(rendered[1])
    assert first_meta["easy_settings"]["auto_curate"] is False
    assert first_meta["bgm_credit"] == PROJECT_KV["bgm_credit"]
    assert first_meta["user_ordered"] is True


def test_rehome_helper_keeps_per_slide_keys():
    a = Slide(id="a", meta=json.dumps({"bgm_fit": {"mode": "trim"}, "bgm_fade_in": 1.0, "easy_place": "숲",
                                       "user_locked": True}))
    b = Slide(id="b", meta=json.dumps({"easy_feel": "바람", "to_real_hold": 1.5}))
    slide_order.rehome_project_meta([b, a])
    assert json.loads(b.meta) == {"easy_feel": "바람", "to_real_hold": 1.5,
                                  "bgm_fit": {"mode": "trim"}, "bgm_fade_in": 1.0}
    assert json.loads(a.meta) == {"easy_place": "숲", "user_locked": True}
